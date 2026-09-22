"""Customer self-service sub-account management (CW-062 desktop follow-up).

``/api/customer/sub-accounts`` lets a **master** customer run the sub-account
lifecycle of its own organisation without the admin lane. Management rides the
customer session fence — a sub-account session is answered 403 — and every row
touched is scoped by ``parent_user_id = caller``: a foreign or unknown id gets
the single 404 ``SUB_ACCOUNT_NOT_FOUND`` (no IDOR oracle).

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
from app.sub_account_quota import (
    MAX_MONTHLY_QUOTA_CREDITS,
    read_quota_used,
    read_quota_used_map,
)

router = APIRouter(prefix="/api/customer/sub-accounts", tags=["customer-sub-accounts"])

# A display name is a human label, not an identity: bound it so a careless
# paste cannot store a megabyte per sub-account.
MAX_DISPLAY_NAME_LENGTH = 64


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


class UpdateSubAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: StrictStr | None = None
    is_active: bool | None = None


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

    A sub-account session is deliberately refused here: sub-accounts never
    manage sub-accounts (only the master owns the organisation).
    """
    row = conn.execute(
        "SELECT is_active, role, account_type, parent_user_id FROM users WHERE id = %s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if row is None or not row[0] or row[1] != "customer":
        raise _http(401, "ACCOUNT_UNAVAILABLE", "账号已停用，请联系管理员。")
    if row[2] != "MASTER" or row[3] is not None:
        raise _http(403, "MASTER_ACCOUNT_REQUIRED", "该操作仅限母账号执行。")


# SELECT column order: (id, username, display_name, account_type,
# parent_user_id, is_active, has_password, created_at, updated_at,
# monthly_quota_credits).
#
# The quota rides as a correlated scalar subquery rather than a LEFT JOIN so
# ``_lock_owned_sub``'s bare ``FOR UPDATE`` keeps locking exactly the intended
# ``users`` row; the cap row is only ever written under that same row lock.
_SUB_COLUMNS = (
    "id, username, display_name, account_type, parent_user_id, is_active, "
    "(password_hash IS NOT NULL), created_at, updated_at, "
    "(SELECT q.monthly_credits FROM sub_account_quotas q WHERE q.user_id = users.id)"
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


def _sub_payload(row: tuple[object, ...], *, used_credits: int = 0) -> dict[str, object]:
    # ``monthly_quota_credits`` = None means unlimited (no quota row). The
    # remaining figure is clamped so a cap lowered below the month's usage
    # renders as "0 left" instead of a negative number.
    quota = None if row[9] is None else int(str(row[9]))
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
        _lock_master(conn, ctx.user_id)
        # ``_SUB_SELECT`` hard-codes the id filter; the list variant needs the
        # parent filter only, so run the dedicated statement instead.
        rows = conn.execute(
            f"SELECT {_SUB_COLUMNS} FROM users WHERE parent_user_id = %s "
            "AND account_type IN ('SUB', 'SUB_ADMIN') "
            "ORDER BY created_at DESC",
            (ctx.user_id,),
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
    """Update display_name / is_active; deactivation revokes the live session."""
    snapshot = _require_snapshot(request)
    if body.display_name is None and body.is_active is None:
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
            metadata={"display_name": display_name, "is_active": body.is_active},
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
    """
    snapshot = _require_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_master(conn, ctx.user_id)
        _lock_owned_sub(conn, master_id=ctx.user_id, sub_account_id=sub_account_id)
        if body.monthly_quota_credits is None:
            conn.execute("DELETE FROM sub_account_quotas WHERE user_id = %s", (sub_account_id,))
        else:
            conn.execute(
                "INSERT INTO sub_account_quotas (user_id, monthly_credits) VALUES (%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "monthly_credits = EXCLUDED.monthly_credits, updated_at = NOW()",
                (sub_account_id, body.monthly_quota_credits),
            )
        row = conn.execute(_SUB_SELECT, (sub_account_id, ctx.user_id)).fetchone()
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
