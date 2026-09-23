"""Sub-account authorization middleware (Phase 2 T2.8-T2.9).

Provides two essential guards for sub-account scoped operations:

1. ensure_sub_account_access(): Validates the caller IS a live sub-account.
   - Reads users.account_type / parent_user_id / is_active on the request connection
   - Asserts account_type in ('SUB', 'SUB_ADMIN'), is_active and a bound parent
   - Returns SubAccountCurrentUser with account_type + parent_user_id

2. prevent_cross_parent_access(): Validates caller only accesses resources under same parent.
   - For master accounts: no restriction (can access own resources)
   - For sub-accounts: all queried resources must have parent_user_id == caller.parent_user_id
   - Prevents one sub-account from accessing another sub-account's data

Usage patterns:
```python
# In routes that require sub-account caller
@router.post("/sub-accounts/{id}/actions")
def do_something(
    current_user: Annotated[SubAccountCurrentUser, Depends(ensure_sub_account_access)]
):
    # Now we know: account_type='SUB', is_active=true
    parent_id = current_user.parent_user_id  # Available
    ...

# In routes that need cross-parent guard (e.g., listing child resources)
@router.get("/devices")
async def list_devices(
    conn: Database,
    current_user: CurrentUser,
    parent_user_id: str = Query(...),
):
    prevent_cross_parent_access(current_user, target_parent_user_id=parent_user_id)
    ...
```

Database assumptions:
- users表 has columns: id, username, display_name, parent_user_id, account_type, is_active
- account_type is TEXT ('MASTER' or 'SUB')
- parent_user_id is TEXT FK→users.id (NULL for masters)
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
from fastapi import HTTPException

from app.auth import AuthenticatedUser, CurrentUser, Database
from app.db_portable import BusinessConnection

# ---------------------------------------------------------------------------
# T2.8: Sub-account access validator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubAccountCurrentUser(CurrentUser):
    """A caller proven to be an active sub-account (T2.8).

    ``account_type`` carries the exact sub kind ('SUB' / 'SUB_ADMIN') and
    ``parent_user_id`` the master account this caller belongs to — the only
    fields a sub-account-scoped route may branch on.
    """

    account_type: str
    parent_user_id: str  # The master account ID this sub-account belongs to


def ensure_sub_account_access(
    conn: Database,
    current_user: AuthenticatedUser,
) -> SubAccountCurrentUser:
    """Validate caller is an active sub-account (T2.8).

    Reads the caller's account row on the request connection and answers 403
    for anything that is not a live SUB/SUB_ADMIN with a bound parent. A
    route wrapping this dependency then wires ``prevent_cross_parent_access``
    before touching any resource (T2.9).

    Raises HTTPException 403 if:
    - Caller is not a sub-account (account_type not in SUB/SUB_ADMIN)
    - Caller is deactivated (is_active == false)
    - Caller has no bound parent_user_id

    Returns:
        SubAccountCurrentUser with account_type + parent_user_id populated

    Usage:
        @router.post("/endpoints")
        def endpoint(
            current_user: Annotated[SubAccountCurrentUser, Depends(ensure_sub_account_access)]
        ):
            # Now current_user is guaranteed SUB + active + parent bound
            parent_id = current_user.parent_user_id
    """
    row = conn.execute(
        "SELECT account_type, parent_user_id, is_active FROM users WHERE id = %s",
        (current_user.id,),
    ).fetchone()
    if row is None or str(row[0]) not in {"SUB", "SUB_ADMIN"} or not row[2] or row[1] is None:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SUB_ACCOUNT_REQUIRED",
                "message": "该操作仅限有效子账号执行。",
            },
        )
    return SubAccountCurrentUser(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        role=current_user.role,
        account_type=str(row[0]),
        parent_user_id=str(row[1]),
    )


# ---------------------------------------------------------------------------
# T2.9: Cross-parent access guard
# ---------------------------------------------------------------------------


def prevent_cross_parent_access(
    current_user: CurrentUser,
    *,
    target_parent_user_id: str | None = None,
    resource_owner_id: str | None = None,
    conn: BusinessConnection | psycopg.Connection | None = None,
) -> None:
    """Prevent sub-accounts from accessing resources outside their parent org.

    Logic:
    - Master accounts (parent_user_id NULL): unrestricted access to their own resources
    - Sub-accounts: all accessed resources MUST have same parent_user_id as caller

    Scenarios:
    1. Sub-account accessing its own devices: OK (same parent)
    2. Sub-account A accessing Sub-account B's publish accounts: DENIED (different parents)
    3. Master account accessing its own resources: OK

    Args:
        current_user: The authenticated caller (must have parent_user_id attribute)
        target_parent_user_id: Optional explicit parent ID to check against
        resource_owner_id: If provided, looks up the resource's parent_user_id from DB

    Raises:
        HTTPException 403 with code "CROSS_PARENT_ACCESS_FORBIDDEN"

    Usage:
        @router.get("/devices")
        async def list_devices(
            current_user: CurrentUser,
            db: Database,
        ):
            # Get target resource's parent (could be from query param or fetched row)
            device_parent = db.execute(
                "SELECT parent_user_id FROM customer_devices WHERE id = %s",
                (device_id,)
            ).fetchone()["parent_user_id"]

            prevent_cross_parent_access(
                current_user,
                target_parent_user_id=device_parent
            )
    """
    # Check if caller is a sub-account
    caller_is_sub = getattr(current_user, "account_type", None) in {"SUB", "SUB_ADMIN"}

    if not caller_is_sub:
        # Masters can access their own resources without restriction
        return

    caller_parent = getattr(current_user, "parent_user_id", None)
    if caller_parent is None:
        # Should never happen: SUB without parent_user_id
        raise HTTPException(
            status_code=500,
            detail={
                "code": "INVALID_SUB_ACCOUNT_STATE",
                "message": "Sub-account missing parent_user_id reference.",
            },
        )

    # Determine which parent to check against
    if target_parent_user_id is not None:
        # Explicit parent ID provided
        target_parent = target_parent_user_id
    elif resource_owner_id is not None:
        # Resolve the resource owner's organisation on the caller's connection:
        # a master-owned row belongs to that master's own org, a sub-owned row
        # to its parent org.
        if conn is None:
            raise ValueError("resource_owner_id lookup requires a connection")
        row = conn.execute(
            "SELECT parent_user_id FROM users WHERE id = %s",
            (resource_owner_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "RESOURCE_OWNER_NOT_FOUND",
                    "message": "目标资源归属账号不存在。",
                },
            )
        target_parent = str(row[0]) if row[0] is not None else str(resource_owner_id)
    else:
        raise ValueError("Must provide either target_parent_user_id or resource_owner_id")

    # Enforce parent equality
    if caller_parent != target_parent:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "CROSS_PARENT_ACCESS_FORBIDDEN",
                "message": (
                    f"子账号只能访问同一母账号下的资源。当前父账号：{caller_parent}, "
                    f"目标资源父账号：{target_parent}"
                ),
            },
        )


# ---------------------------------------------------------------------------
# Shared password-lane admission (login + session fence)
# ---------------------------------------------------------------------------


# Account origins that authenticate with a username + password of their own.
_PASSWORD_SOURCES = frozenset({"self_register", "activation_code"})


def password_login_account_ok(
    conn: psycopg.Connection,
    *,
    registration_source: str | None,
    account_type: str | None,
    parent_user_id: str | None,
) -> bool:
    """The shared admission rule for password-authenticated accounts.

    Used by both ``POST /api/customer/password-login`` and the session fence
    (``verify_session_context``) so login and every fenced write agree on who
    may hold a password session:

    - ``self_register`` / ``activation_code`` masters authenticate on their
      own credentials (the pre-sub-account lanes);
    - a sub-account is admitted only with an admin-issued password
      (``registration_source='admin_create'``, written by the sub-account
      creation/reset endpoints), a bound parent and an *active* master —
      deactivating the master freezes every session riding under it.
    """
    if registration_source in _PASSWORD_SOURCES:
        return True
    if (
        registration_source == "admin_create"
        and account_type in {"SUB", "SUB_ADMIN"}
        and parent_user_id is not None
    ):
        parent = conn.execute(
            "SELECT is_active FROM users WHERE id = %s", (parent_user_id,)
        ).fetchone()
        return parent is not None and bool(parent[0])
    return False


# ---------------------------------------------------------------------------
# Helper functions for integration
# ---------------------------------------------------------------------------


def get_sub_account_parent(conn: BusinessConnection, sub_account_id: str) -> str:
    """Fetch parent_user_id for a sub-account (for use in middlewares)."""
    row = conn.execute(
        "SELECT parent_user_id, account_type, is_active FROM users WHERE id = %s",
        (sub_account_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Sub-account {sub_account_id} not found")

    if row["account_type"] != "SUB":
        raise ValueError(f"User {sub_account_id} is not a sub-account")

    parent_id = row["parent_user_id"]
    if parent_id is None:
        raise ValueError(f"Sub-account {sub_account_id} has no parent bound")

    return str(parent_id)


def validate_sub_account_active(conn: BusinessConnection, sub_account_id: str) -> bool:
    """Check if sub-account is active."""
    row = conn.execute(
        "SELECT is_active FROM users WHERE id = %s AND account_type = 'SUB'",
        (sub_account_id,),
    ).fetchone()

    if row is None:
        return False

    return bool(row["is_active"])
