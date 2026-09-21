"""Sub-account management APIs (Phase 2 MVP).

Endpoints for creating, listing, updating and deleting sub-accounts under master accounts.
Design decisions:
- Sub-accounts are users.parent_user_id → ON DELETE CASCADE (set in Phase 1)
- Parent must be MASTER + role='customer' (enforced here)
- Account status reuses users.is_active (integer 0/1 CHECK), not a new column
- T2.10 actor tracking: mother consumes → actor=user_id; sub consumes → actor=sub_account_id

Security & audit:
- CW-026/027 管理面守卫：读写分离走 AdminReader / AdminWriter（管理端会话 + 写 CSRF）
- Cascade awareness: parent delete cascades to subs/devices/publish accounts
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
from app.auth import Database
from app.db_portable import BusinessConnection

router = APIRouter(prefix="/api/admin", tags=["sub-accounts"])


class CreateSubAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: StrictStr
    display_name: StrictStr
    parent_user_id: str  # The master account ID
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


def _fetch_sub_account(conn: BusinessConnection, sub_account_id: str) -> dict[str, object]:
    """Load one SUB account row as the API response shape (is_active → bool)."""
    row = conn.execute(
        "SELECT id, username, display_name, parent_user_id, is_active, "
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
        row = conn.execute(
            """
            INSERT INTO users (id, username, display_name, parent_user_id,
                              account_type, role, is_active)
            VALUES (%s, %s, %s, %s, 'SUB', 'customer', 1)
            RETURNING created_at
            """,
            (sub_id, body.username, body.display_name, body.parent_user_id),
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


@router.delete("/sub-accounts/{sub_account_id}", status_code=200)
def delete_sub_account(
    sub_account_id: str,
    conn: Database,
    admin: AdminWriter,
) -> dict[str, object]:
    """Delete a sub-account (cascades to devices/accounts via FK)."""
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

    # Delete (FK cascade handles devices/publish accounts)
    conn.execute(
        "DELETE FROM users WHERE id = %s AND account_type = 'SUB'",
        (sub_account_id,),
    )
    _write_sub_account_audit(
        conn,
        actor_user_id=admin.user_id,
        action="sub_account.delete",
        entity_id=sub_account_id,
        reason="sub-account deleted",
    )

    return {"deleted": True, "sub_account_id": sub_account_id}
