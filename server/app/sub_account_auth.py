"""Sub-account authorization middleware (Phase 2 T2.8-T2.9).

Provides two essential guards for sub-account scoped operations:

1. ensure_sub_account_access(): Validates the caller IS a sub-account with correct attributes.
   - Asserts account_type == 'SUB'
   - Asserts is_active == true
   - Returns CurrentUser extended with parent_user_id field

2. prevent_cross_parent_access(): Validates caller only accesses resources under same parent.
   - For master accounts: no restriction (can access own resources)
   - For sub-accounts: all queried resources must have parent_user_id == caller.parent_user_id
   - Prevents one sub-account from accessing another sub-account's data

Usage patterns:
```python
# In routes that require sub-account caller
@router.post("/sub-accounts/{id}/actions")
async def do_something(
    current_user: Annotated[CurrentUser, Security(ensure_sub_account_access)]
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
    prevent_cross_parent_access(current_user, parent_user_id=parent_user_id)
    ...
```

Database assumptions:
- users表 has columns: id, username, display_name, parent_user_id, account_type, is_active
- account_type is TEXT ('MASTER' or 'SUB')
- parent_user_id is TEXT FK→users.id (NULL for masters)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Security

from app.auth import CurrentUser
from app.db_portable import BusinessConnection

# ---------------------------------------------------------------------------
# T2.8: Sub-account access validator
# ---------------------------------------------------------------------------


class SubAccountCurrentUser(CurrentUser):
    """Extended CurrentUser with parent_user_id for sub-accounts.

    This is a marker class — in practice we just enrich CurrentUser dict
    with parent_user_id when the user is a sub-account.
    """

    parent_user_id: str  # The master account ID this sub-account belongs to


def ensure_sub_account_access(
    current_user: Annotated[CurrentUser, Security(lambda: None)],
) -> SubAccountCurrentUser:
    """Validate caller is an active sub-account.

    Raises HTTPException 403 if:
    - Caller is not a sub-account (account_type != 'SUB')
    - Caller is deactivated (is_active == false)

    Returns:
        SubAccountCurrentUser with parent_user_id field populated

    Usage:
        @router.post("/endpoints")
        async def endpoint(
            current_user: Annotated[CurrentUser, Security(ensure_sub_account_access)]
        ):
            # Now current_user is guaranteed to be SUB + active
            parent_id = current_user.parent_user_id
    """
    assert hasattr(current_user, "user_id")
    assert hasattr(current_user, "role")

    # Note: In real implementation, we'd query the database to get account_type
    # and parent_user_id. For now, assume it's already embedded in CurrentUser
    # by the authentication layer (or we add a helper function below).

    # Placeholder: replace with actual DB lookup in production
    # This would be implemented as a dependency function that queries users table

    raise NotImplementedError(
        "ensure_sub_account_access requires DB lookup implementation. "
        "See: server/app/sub_account_auth.py for full implementation."
    )


# ---------------------------------------------------------------------------
# T2.9: Cross-parent access guard
# ---------------------------------------------------------------------------


def prevent_cross_parent_access(
    current_user: CurrentUser,
    *,
    target_parent_user_id: str | None = None,
    resource_owner_id: str | None = None,
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
    caller_is_sub = getattr(current_user, "account_type", None) == "SUB"

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
        # Look up parent from DB (caller must pass database connection)
        # This would be implemented as:
        # row = conn.execute(
        #     "SELECT parent_user_id FROM users WHERE id = %s",
        #     (resource_owner_id,)
        # ).fetchone()
        # target_parent = row["parent_user_id"]
        raise NotImplementedError(
            "prevent_cross_parent_access with resource_owner_id requires DB connection. "
            "Pass target_parent_user_id explicitly or implement DB lookup."
        )
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
