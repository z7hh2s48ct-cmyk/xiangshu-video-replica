"""Customer self-service sub-account management (CW-062 desktop follow-up).

``/api/customer/sub-accounts`` lets a **master** customer run the sub-account
lifecycle of its own organisation without the admin lane. Management rides the
customer session fence and splits in two scopes (Phase 3b):

- **Master only** — create, delete, password reset, rename, activation and
  role changes: only the organisation owner alters the organisation's shape.
- **Master or granted SUB_ADMIN** — listing, quota and feature-permission
  configuration. A SUB_ADMIN may only touch plain ``SUB`` rows (never another
  admin, never itself); queries scope by the organisation's
  ``parent_user_id`` either way, so a foreign or unknown id gets the single
  404 ``SUB_ACCOUNT_NOT_FOUND`` (no IDOR oracle).

Feature permissions (Phase 3b) follow the quota row's shape: no row means
unrestricted, and saving the full grant deletes the row — 'unrestricted' has
exactly one spelling. ``permissions`` on the payload is ``None`` for an
unrestricted sub-account.

Lifecycle rules (mirroring the admin lane, plus the customer-lane safety rails):

- Creation accepts an optional initial password. With one the sub can log in
  immediately (``registration_source='admin_create'`` — the origin the shared
  password-session rule requires); without one the account exists but cannot
  hold a session until ``POST .../password`` sets one.
- Deactivation (``is_active=false``) revokes the sub's live session in the same
  transaction (SES-03 propagation): the credential stops working the moment
  the master says so.
- Setting/rotating the password revokes the live session too — rotation is the
  leak response and an old session must not outlive the old password.
- DELETE prefers a real delete: the session-state/device footprint is purged
  first, then the row. History pins the account instead — business rows (the
  ledger, tasks) whose FKs carry no CASCADE, and the append-only session-event
  log (029) that is never rewritten. Such an account degrades to deactivation
  + revocation and the answer is ``deleted: false`` instead of failing.

Audit rows land in ``audit_logs`` (action prefix ``customer.sub_account.``)
with public metadata only; plaintext passwords are never persisted or echoed.
"""

from __future__ import annotations

import json
import uuid
from typing import Literal

import psycopg
from fastapi import APIRouter, Request
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.admin_write_contract import transaction_now_iso
from app.api_errors import http_error as _http
from app.customer_auth_routes import USERS_USERNAME_CONSTRAINT, _normalize_username
from app.customer_fence import (
    CustomerSessionSnapshot,
    customer_session_snapshot,
    fenced_pg_transaction,
)
from app.customer_session_service import (
    REASON_SUB_ACCOUNT_DEACTIVATED,
    REASON_SUB_ACCOUNT_PASSWORD_RESET,
    SUB_ACCOUNT_FOOTPRINT_TABLES,
    revoke_session,
)
from app.password_hashing import PasswordPolicyError, hash_password
from app.sub_account_permissions import is_all_permissions, normalize_businesses
from app.sub_account_quota import (
    MAX_MONTHLY_QUOTA_CREDITS,
    read_quota_used,
    read_quota_used_map,
)

router = APIRouter(prefix="/api/customer/sub-accounts", tags=["customer-sub-accounts"])

# A display name is a human label, not an identity: bound it so a careless
# paste cannot store a megabyte per sub-account.
MAX_DISPLAY_NAME_LENGTH = 64


class SetSubAccountPermissionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # All three keys are required on purpose so a partial body never widens
    # access by omission; the full grant (12 businesses + both switches) is
    # the canonical "unrestricted" spelling and deletes the row.
    businesses: list[StrictStr]
    allow_api_keys: bool
    allow_publish_accounts: bool


class CreateSubAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: StrictStr
    display_name: StrictStr
    # Optional initial credential: with a password the sub-account can log in
    # immediately; without one it exists but cannot hold a session until a
    # password is set through the reset endpoint.
    password: StrictStr | None = None
    # Optional monthly spend cap in credits; omitted = unlimited, matching
    # every pre-quota sub-account (the ``sub_account_quotas`` row is the cap).
    monthly_quota_credits: int | None = Field(default=None, ge=0, le=MAX_MONTHLY_QUOTA_CREDITS)
    # Optional initial feature permissions; omitted = unrestricted. Accepted
    # at creation so the account never exists (even briefly) with broader
    # rights than its creator granted — one atomic transaction, no follow-up
    # PUT that could fail on its own.
    permissions: SetSubAccountPermissionsRequest | None = None


class UpdateSubAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: StrictStr | None = None
    is_active: bool | None = None
    # Role change (Phase 3b): promote a plain SUB to SUB_ADMIN or demote it
    # back. Master only — this endpoint already is.
    account_type: Literal["SUB", "SUB_ADMIN"] | None = None


class SetSubAccountPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: StrictStr


class SetSubAccountQuotaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required on purpose: ``null`` clears the cap (the sub-account becomes
    # unlimited again), while a missing key is a client bug — never a silent
    # clear.
    monthly_quota_credits: int | None = Field(ge=0, le=MAX_MONTHLY_QUOTA_CREDITS)


def _require_snapshot(request: Request) -> CustomerSessionSnapshot:
    """Management is session-fenced: no session token, no sub-account lane."""
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise _http(
            401,
            "SESSION_REQUIRED",
            "A customer session token is required.",
        )
    return snapshot


def _request_id(request: Request) -> str:
    """The event/audit trace id: the caller's Idempotency-Key when present."""
    header = request.headers.get("Idempotency-Key", "").strip()
    return header[:200] if header else str(uuid.uuid4())


def _normalize_display_name(raw: str) -> str:
    display_name = raw.strip()
    if not display_name or len(display_name) > MAX_DISPLAY_NAME_LENGTH:
        raise _http(
            400,
            "INVALID_DISPLAY_NAME",
            f"display_name must contain 1 to {MAX_DISPLAY_NAME_LENGTH} characters",
        )
    return display_name


def _hash_password_boundary(password: str) -> str:
    """Hash at the boundary; a policy violation is the stable 400 code."""
    try:
        return hash_password(password)
    except PasswordPolicyError as exc:
        raise _http(400, "WEAK_PASSWORD", str(exc)) from exc


def _lock_master(conn: psycopg.Connection, user_id: str) -> None:
    """Lock the caller's row and enforce the master-only contract.

    Structural changes (create/delete/password/rename/activation/role) stay
    with the organisation owner: a SUB_ADMIN is deliberately refused here —
    its grant covers configuring plain SUB rows, never the org's shape.
    """
    row = conn.execute(
        "SELECT is_active, role, account_type, parent_user_id FROM users WHERE id = %s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if row is None or not row[0] or row[1] != "customer":
        raise _http(401, "ACCOUNT_UNAVAILABLE", "账号已停用，请联系管理员。")
    if row[2] != "MASTER" or row[3] is not None:
        raise _http(403, "MASTER_ACCOUNT_REQUIRED", "该操作仅限母账号执行。")


def _lock_org_admin(conn: psycopg.Connection, user_id: str) -> tuple[str, str]:
    """Lock the caller's row; master or a granted SUB_ADMIN pass, others 403.

    Returns ``(account_type, organisation_id)``: a SUB_ADMIN's organisation
    is its own parent, so every org-scoped query keeps one owner column.
    Plain SUB sessions are refused with the same stable code the master-only
    lane has always answered — the caller needs its master (or an admin).
    """
    row = conn.execute(
        "SELECT is_active, role, account_type, parent_user_id FROM users WHERE id = %s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if row is None or not row[0] or row[1] != "customer":
        raise _http(401, "ACCOUNT_UNAVAILABLE", "账号已停用，请联系管理员。")
    if row[2] == "MASTER" and row[3] is None:
        return "MASTER", user_id
    if row[2] == "SUB_ADMIN" and row[3] is not None:
        return "SUB_ADMIN", str(row[3])
    raise _http(403, "MASTER_ACCOUNT_REQUIRED", "该操作仅限母账号执行。")


def _require_plain_sub_scope(caller_type: str, current: tuple[object, ...]) -> None:
    """A SUB_ADMIN may only configure plain SUB rows — admins are master-only."""
    if caller_type != "MASTER" and str(current[3]) != "SUB":
        raise _http(403, "MASTER_ACCOUNT_REQUIRED", "该操作仅限母账号执行。")


# SELECT column order: (id, username, display_name, account_type,
# parent_user_id, is_active, has_password, created_at, updated_at,
# monthly_quota_credits, allowed_businesses, allow_api_keys,
# allow_publish_accounts).
#
# The quota and permission rows ride as correlated scalar subqueries rather
# than LEFT JOINs so ``_lock_owned_sub``'s bare ``FOR UPDATE`` keeps locking
# exactly the intended ``users`` row (a join's NULL-extended side cannot be
# row-locked in PG); both side rows are only ever written under that same
# ``users`` row lock.
_SUB_COLUMNS = (
    "id, username, display_name, account_type, parent_user_id, is_active, "
    "(password_hash IS NOT NULL), created_at, updated_at, "
    "(SELECT q.monthly_credits FROM sub_account_quotas q WHERE q.user_id = users.id), "
    "(SELECT p.allowed_businesses FROM sub_account_permissions p WHERE p.user_id = users.id), "
    "(SELECT p.allow_api_keys FROM sub_account_permissions p WHERE p.user_id = users.id), "
    "(SELECT p.allow_publish_accounts FROM sub_account_permissions p WHERE p.user_id = users.id)"
)
_SUB_SELECT = (
    f"SELECT {_SUB_COLUMNS} FROM users WHERE id = %s AND parent_user_id = %s "
    "AND account_type IN ('SUB', 'SUB_ADMIN')"
)


def _lock_owned_sub(
    conn: psycopg.Connection, *, master_id: str, sub_account_id: str
) -> tuple[object, ...]:
    """Lock the sub row scoped to its parent; a foreign/unknown id is the 404."""
    row = conn.execute(_SUB_SELECT + " FOR UPDATE", (sub_account_id, master_id)).fetchone()
    if row is None:
        raise _http(404, "SUB_ACCOUNT_NOT_FOUND", "子账号不存在。")
    return row


def _validated_businesses(values: list[str]) -> list[str]:
    """Business keys are a closed vocabulary: an unknown key is the stable 422."""
    try:
        return normalize_businesses(values)
    except ValueError as exc:
        raise _http(422, "INVALID_BUSINESS_PERMISSIONS", str(exc)) from exc


def _write_permissions(
    conn: psycopg.Connection,
    *,
    sub_account_id: str,
    businesses: list[str],
    allow_api_keys: bool,
    allow_publish_accounts: bool,
) -> None:
    """Persist feature permissions with one canonical 'unrestricted' spelling.

    The full grant deletes the row (no row == unrestricted); anything
    narrower upserts. Keeps 'no row' and 'all granted' from drifting apart.
    """
    if is_all_permissions(
        businesses=businesses,
        allow_api_keys=allow_api_keys,
        allow_publish_accounts=allow_publish_accounts,
    ):
        conn.execute("DELETE FROM sub_account_permissions WHERE user_id = %s", (sub_account_id,))
        return
    conn.execute(
        "INSERT INTO sub_account_permissions "
        "(user_id, allowed_businesses, allow_api_keys, allow_publish_accounts) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (user_id) DO UPDATE SET "
        "allowed_businesses = EXCLUDED.allowed_businesses, "
        "allow_api_keys = EXCLUDED.allow_api_keys, "
        "allow_publish_accounts = EXCLUDED.allow_publish_accounts, "
        "updated_at = NOW()",
        (
            sub_account_id,
            json.dumps(businesses, ensure_ascii=True),
            allow_api_keys,
            allow_publish_accounts,
        ),
    )


def _sub_payload(row: tuple[object, ...], *, used_credits: int = 0) -> dict[str, object]:
    # ``monthly_quota_credits`` = None means unlimited (no quota row). The
    # remaining figure is clamped so a cap lowered below the month's usage
    # renders as "0 left" instead of a negative number.
    quota = None if row[9] is None else int(str(row[9]))
    # ``permissions`` = None means unrestricted (no permission row), the same
    # shape contract as the quota above.
    permissions = None
    if row[10] is not None:
        raw_businesses = row[10]
        if isinstance(raw_businesses, str):  # TEXT-JSON column (JSON lives in TEXT)
            raw_businesses = json.loads(raw_businesses)
        # ``permissions`` stores a JSON array; narrow to list so mypy sees an
        # iterable and a malformed payload degrades to empty instead of raising.
        businesses_list = raw_businesses if isinstance(raw_businesses, list) else []
        permissions = {
            "businesses": [str(key) for key in businesses_list],
            "allow_api_keys": bool(row[11]),
            "allow_publish_accounts": bool(row[12]),
        }
    return {
        "id": str(row[0]),
        "username": str(row[1]),
        "display_name": row[2],
        "account_type": str(row[3]),
        "parent_user_id": str(row[4]),
        "is_active": bool(row[5]),
        "has_password": bool(row[6]),
        "created_at": row[7],
        "updated_at": row[8],
        "monthly_quota_credits": quota,
        "quota_used_credits": used_credits,
        "quota_remaining_credits": None if quota is None else max(0, quota - used_credits),
        "permissions": permissions,
    }


def _write_audit(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    action: str,
    entity_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    """Append one sub-account lifecycle audit row (public metadata only)."""
    conn.execute(
        "INSERT INTO audit_logs "
        "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
        "VALUES (%s, %s, %s, 'user', %s, %s)",
        (
            str(uuid.uuid4()),
            actor_user_id,
            action,
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


@router.get("")
def list_sub_accounts(request: Request) -> dict[str, object]:
    """List the sub-accounts of the caller's own organisation."""
    snapshot = _require_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _, org_id = _lock_org_admin(conn, ctx.user_id)
        # ``_SUB_SELECT`` hard-codes the id filter; the list variant needs the
        # parent filter only, so run the dedicated statement instead.
        rows = conn.execute(
            f"SELECT {_SUB_COLUMNS} FROM users WHERE parent_user_id = %s "
            "AND account_type IN ('SUB', 'SUB_ADMIN') "
            "ORDER BY created_at DESC",
            (org_id,),
        ).fetchall()
        used = read_quota_used_map(conn, [str(row[0]) for row in rows])
        items = [_sub_payload(row, used_credits=used.get(str(row[0]), 0)) for row in rows]
    return {"sub_accounts": items, "total_count": len(items)}


@router.post("", status_code=201)
def create_sub_account(body: CreateSubAccountRequest, request: Request) -> dict[str, object]:
    """Create one sub-account under the caller's own master account."""
    snapshot = _require_snapshot(request)
    username = _normalize_username(body.username)
    display_name = _normalize_display_name(body.display_name)

    # Hash BEFORE opening the transaction (mirrors the public register lane):
    # scrypt is deliberately CPU/memory heavy and must not shrink the pool.
    password_hash: str | None = None
    registration_source: str | None = None
    if body.password is not None:
        password_hash = _hash_password_boundary(body.password)
        registration_source = "admin_create"

    sub_account_id = f"sub-{uuid.uuid4().hex[:12]}"
    # Validated outside the transaction: an unknown key is a 422 long before
    # any row is touched. Omitted = unrestricted (no permission row).
    permissions = body.permissions
    businesses = _validated_businesses(permissions.businesses) if permissions else []
    try:
        with fenced_pg_transaction(snapshot) as (conn, ctx):
            _lock_master(conn, ctx.user_id)
            taken = conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone()
            if taken is not None:
                raise _http(409, "USERNAME_TAKEN", "该用户名已被占用。")
            conn.execute(
                "INSERT INTO users (id, username, display_name, parent_user_id, "
                "account_type, role, is_active, password_hash, registration_source) "
                "VALUES (%s, %s, %s, %s, 'SUB', 'customer', 1, %s, %s)",
                (
                    sub_account_id,
                    username,
                    display_name,
                    ctx.user_id,
                    password_hash,
                    registration_source,
                ),
            )
            if body.monthly_quota_credits is not None:
                conn.execute(
                    "INSERT INTO sub_account_quotas (user_id, monthly_credits) VALUES (%s, %s)",
                    (sub_account_id, body.monthly_quota_credits),
                )
            if permissions is not None:
                # Written in the same transaction as the account: it never
                # exists with broader rights than its creator granted.
                _write_permissions(
                    conn,
                    sub_account_id=sub_account_id,
                    businesses=businesses,
                    allow_api_keys=permissions.allow_api_keys,
                    allow_publish_accounts=permissions.allow_publish_accounts,
                )
            row = conn.execute(_SUB_SELECT, (sub_account_id, ctx.user_id)).fetchone()
            _write_audit(
                conn,
                actor_user_id=ctx.user_id,
                action="customer.sub_account.create",
                entity_id=sub_account_id,
                metadata={
                    "username": username,
                    "has_password": password_hash is not None,
                    "monthly_quota_credits": body.monthly_quota_credits,
                    "permissions": None
                    if permissions is None
                    else {
                        "businesses": businesses,
                        "allow_api_keys": permissions.allow_api_keys,
                        "allow_publish_accounts": permissions.allow_publish_accounts,
                    },
                },
            )
    except UniqueViolation as exc:
        # The pre-check above answers the friendly 409; this catch is the race
        # backstop on the ``users_username_key`` index, never a 500.
        constraint = getattr(getattr(exc, "diag", None), "constraint_name", None)
        if constraint == USERS_USERNAME_CONSTRAINT:
            raise _http(409, "USERNAME_TAKEN", "该用户名已被占用。") from exc
        raise
    if row is None:  # pragma: no cover - the INSERT above just succeeded
        raise _http(500, "SUB_ACCOUNT_NOT_CREATED", "子账号创建失败，请重试。")
    return _sub_payload(row)


@router.patch("/{sub_account_id}")
def update_sub_account(
    sub_account_id: str, body: UpdateSubAccountRequest, request: Request
) -> dict[str, object]:
    """Update display_name / is_active / account_type; deactivation revokes the session."""
    snapshot = _require_snapshot(request)
    if body.display_name is None and body.is_active is None and body.account_type is None:
        raise _http(400, "EMPTY_UPDATE", "请提供要修改的字段。")
    display_name = (
        _normalize_display_name(body.display_name) if body.display_name is not None else None
    )

    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_master(conn, ctx.user_id)
        current = _lock_owned_sub(conn, master_id=ctx.user_id, sub_account_id=sub_account_id)
        was_active = bool(current[5])

        updates: list[str] = []
        params: list[object] = []
        if display_name is not None:
            updates.append("display_name = %s")
            params.append(display_name)
        if body.is_active is not None:
            updates.append("is_active = %s")
            params.append(1 if body.is_active else 0)
        if body.account_type is not None:
            updates.append("account_type = %s")
            params.append(body.account_type)
        params.append(sub_account_id)
        conn.execute(
            f"UPDATE users SET {', '.join(updates)}, updated_at = NOW() WHERE id = %s",
            params,
        )
        if body.is_active is False and was_active:
            # SES-03 propagation: an account the master just disabled must not
            # keep riding a live session until its lease lapses.
            revoke_session(
                conn,
                user_id=sub_account_id,
                actor_user_id=ctx.user_id,
                reason=REASON_SUB_ACCOUNT_DEACTIVATED,
                request_id=_request_id(request),
                now_iso=transaction_now_iso(conn),
            )
        row = conn.execute(_SUB_SELECT, (sub_account_id, ctx.user_id)).fetchone()
        used = read_quota_used(conn, sub_account_id)
        _write_audit(
            conn,
            actor_user_id=ctx.user_id,
            action="customer.sub_account.update",
            entity_id=sub_account_id,
            metadata={
                "display_name": display_name,
                "is_active": body.is_active,
                "account_type": body.account_type,
            },
        )
    if row is None:  # pragma: no cover - the row lock above just found it
        raise _http(404, "SUB_ACCOUNT_NOT_FOUND", "子账号不存在。")
    return _sub_payload(row, used_credits=used)


@router.post("/{sub_account_id}/password")
def set_sub_account_password(
    sub_account_id: str, body: SetSubAccountPasswordRequest, request: Request
) -> dict[str, object]:
    """Set or rotate a sub-account's login password.

    Writing ``registration_source='admin_create'`` alongside the hash is what
    admits the account on the shared password-session rule. The rotation also
    revokes the live session: the old session must not outlive the old
    credential. Plaintext never touches the database or the audit trail.
    """
    snapshot = _require_snapshot(request)
    password_hash = _hash_password_boundary(body.password)

    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_master(conn, ctx.user_id)
        _lock_owned_sub(conn, master_id=ctx.user_id, sub_account_id=sub_account_id)
        conn.execute(
            "UPDATE users SET password_hash = %s, registration_source = 'admin_create', "
            "updated_at = NOW() WHERE id = %s",
            (password_hash, sub_account_id),
        )
        revoke_session(
            conn,
            user_id=sub_account_id,
            actor_user_id=ctx.user_id,
            reason=REASON_SUB_ACCOUNT_PASSWORD_RESET,
            request_id=_request_id(request),
            now_iso=transaction_now_iso(conn),
        )
        _write_audit(
            conn,
            actor_user_id=ctx.user_id,
            action="customer.sub_account.password.reset",
            entity_id=sub_account_id,
        )
    return {"id": sub_account_id, "has_password": True}


@router.put("/{sub_account_id}/quota")
def set_sub_account_quota(
    sub_account_id: str, body: SetSubAccountQuotaRequest, request: Request
) -> dict[str, object]:
    """Set (or, with an explicit null, clear) the monthly spend cap.

    A number caps the sub-account's credit consumption for the current
    Shanghai calendar month; ``null`` deletes the row and the sub-account is
    unlimited again. Enforcement lives where the money moves —
    ``accept_operation`` under the actor's billing lock — so this endpoint
    owns the configuration row only. The billing lock does not span this
    transaction: at most one already-accepted, still in-flight operation can
    land under the previous cap; every later one re-reads the new value.
    A SUB_ADMIN caller may set plain SUB caps only (its grant's scope).
    """
    snapshot = _require_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        caller_type, org_id = _lock_org_admin(conn, ctx.user_id)
        current = _lock_owned_sub(conn, master_id=org_id, sub_account_id=sub_account_id)
        _require_plain_sub_scope(caller_type, current)
        if body.monthly_quota_credits is None:
            conn.execute("DELETE FROM sub_account_quotas WHERE user_id = %s", (sub_account_id,))
        else:
            conn.execute(
                "INSERT INTO sub_account_quotas (user_id, monthly_credits) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "monthly_credits = EXCLUDED.monthly_credits, updated_at = NOW()",
                (sub_account_id, body.monthly_quota_credits),
            )
        row = conn.execute(_SUB_SELECT, (sub_account_id, org_id)).fetchone()
        used = read_quota_used(conn, sub_account_id)
        _write_audit(
            conn,
            actor_user_id=ctx.user_id,
            action="customer.sub_account.quota.set",
            entity_id=sub_account_id,
            metadata={"monthly_quota_credits": body.monthly_quota_credits},
        )
    if row is None:  # pragma: no cover - the row lock above just found it
        raise _http(404, "SUB_ACCOUNT_NOT_FOUND", "子账号不存在。")
    return _sub_payload(row, used_credits=used)


@router.put("/{sub_account_id}/permissions")
def set_sub_account_permissions(
    sub_account_id: str, body: SetSubAccountPermissionsRequest, request: Request
) -> dict[str, object]:
    """Set (or, with the full grant, clear) the sub-account's feature permissions.

    Saving the unrestricted value deletes the row — 'unrestricted' has
    exactly one spelling. Enforcement lives at the feature doors themselves
    (``accept_operation``, the Token lane, publish-account binding); this
    endpoint owns the configuration row only. A SUB_ADMIN caller may
    configure plain SUB rows only — the admin grant itself stays
    master-only business.
    """
    snapshot = _require_snapshot(request)
    businesses = _validated_businesses(body.businesses)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        caller_type, org_id = _lock_org_admin(conn, ctx.user_id)
        current = _lock_owned_sub(conn, master_id=org_id, sub_account_id=sub_account_id)
        _require_plain_sub_scope(caller_type, current)
        _write_permissions(
            conn,
            sub_account_id=sub_account_id,
            businesses=businesses,
            allow_api_keys=body.allow_api_keys,
            allow_publish_accounts=body.allow_publish_accounts,
        )
        row = conn.execute(_SUB_SELECT, (sub_account_id, org_id)).fetchone()
        used = read_quota_used(conn, sub_account_id)
        _write_audit(
            conn,
            actor_user_id=ctx.user_id,
            action="customer.sub_account.permissions.set",
            entity_id=sub_account_id,
            metadata={
                "businesses": businesses,
                "allow_api_keys": body.allow_api_keys,
                "allow_publish_accounts": body.allow_publish_accounts,
            },
        )
    if row is None:  # pragma: no cover - the row lock above just found it
        raise _http(404, "SUB_ACCOUNT_NOT_FOUND", "子账号不存在。")
    return _sub_payload(row, used_credits=used)


@router.delete("/{sub_account_id}")
def delete_sub_account(sub_account_id: str, request: Request) -> dict[str, object]:
    """Delete a sub-account, degrading to deactivation when history pins it.

    The session-state/device footprint is purged inside a savepoint, then the
    row itself. A ``ForeignKeyViolation`` there means history refuses to be
    orphaned — business rows (ledger, tasks) or the append-only session-event
    log (029): the savepoint rolls back and the account is deactivated + its
    session revoked instead, so the response carries ``deleted: false``
    rather than failing the request.
    """
    snapshot = _require_snapshot(request)
    deleted = False
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_master(conn, ctx.user_id)
        _lock_owned_sub(conn, master_id=ctx.user_id, sub_account_id=sub_account_id)
        try:
            with conn.transaction():  # savepoint: a pinned row leaves the outer tx usable
                for table in SUB_ACCOUNT_FOOTPRINT_TABLES:
                    conn.execute(f"DELETE FROM {table} WHERE user_id = %s", (sub_account_id,))
                conn.execute(
                    "DELETE FROM users WHERE id = %s AND parent_user_id = %s",
                    (sub_account_id, ctx.user_id),
                )
            deleted = True
        except ForeignKeyViolation:
            revoke_session(
                conn,
                user_id=sub_account_id,
                actor_user_id=ctx.user_id,
                reason=REASON_SUB_ACCOUNT_DEACTIVATED,
                request_id=_request_id(request),
                now_iso=transaction_now_iso(conn),
            )
            conn.execute(
                "UPDATE users SET is_active = 0, updated_at = NOW() "
                "WHERE id = %s AND parent_user_id = %s",
                (sub_account_id, ctx.user_id),
            )
        _write_audit(
            conn,
            actor_user_id=ctx.user_id,
            action="customer.sub_account.delete",
            entity_id=sub_account_id,
            metadata={"deleted": deleted},
        )
    return {"id": sub_account_id, "deleted": deleted, "is_active": False}
