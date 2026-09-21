"""Independent customer device sessions with PostgreSQL leases and fencing.

Each device owns one session row, token, monotonic epoch and 90-second lease.
Login or recovery on one device never changes another device's session. The
legacy switch endpoint remains compatible and follows the same policy.
Heartbeat renews only a matching live session; logout expires only that session.
Administrator account revocation can revoke all matching sessions, while device
revocation is scoped to its own session. Events remain append-only and tokens
are stored as keyed digests. Routes use the PostgreSQL clock.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from app.customer_device_service import (
    _token_digests,
    highest_device_domain_key,
    keyed_digest,
)

# SES-01: heartbeat every 30 seconds, lease 90 seconds (the frozen contract;
# the activation route already grants the first lease with this constant).
SESSION_LEASE_SECONDS = 90

# Login state-machine outcomes.
LOGIN_CREATED = "created"  # new session established (no row / lapsed / recovery)
LOGIN_RENEWED = "renewed"  # same device + valid token: renewed in place
LOGIN_CONFLICT = "conflict"  # another device holds a live lease

# Heartbeat outcomes.
HEARTBEAT_OK = "ok"
HEARTBEAT_REPLACED = "replaced"
HEARTBEAT_EXPIRED = "expired"

# Logout outcomes.
LOGOUT_OK = "ok"
LOGOUT_REPLACED = "replaced"
LOGOUT_EXPIRED = "expired"

# The session-event reason recorded on user-initiated logout.
LOGOUT_REASON = "user_logout"

# The session-event reason recorded when an explicit switch replaces a live
# session (dev doc §12.3 fifth line).
SWITCH_REASON = "explicit_switch"


def mask_device_name(display_name: str) -> str:
    """Mask a device display name for safe client exposure (§13.2).

    At most the first two characters survive; anything shorter is a single
    asterisk. An empty name has nothing to leak and stays empty. The masked
    hint must never leak the full name.
    """
    if not display_name:
        return ""
    if len(display_name) <= 2:
        return "*"
    return display_name[:2] + "**"


@dataclass(frozen=True)
class LoginResult:
    """The outcome of one login attempt (§12.3)."""

    outcome: str
    session_token: str | None
    session_id: str
    session_epoch: int
    lease_until: str
    # Conflict extras (masked hint + remaining lease, dev doc §13.2).
    online_device_name_masked: str | None = None
    online_slot_no: int | None = None
    online_lease_expires_at: str | None = None


@dataclass(frozen=True)
class HeartbeatResult:
    """The outcome of one heartbeat attempt."""

    outcome: str
    session_id: str | None = None
    session_epoch: int | None = None
    lease_until: str | None = None


@dataclass(frozen=True)
class LogoutResult:
    """The outcome of one logout attempt."""

    outcome: str
    session_id: str | None = None
    session_epoch: int | None = None


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------

# SELECT column order shared by login/heartbeat/logout row probes:
# (user_id, activation_code_id, session_id, session_epoch, lease_until,
#  device_id, token_digest)
_ROW_SQL = (
    "SELECT user_id, activation_code_id, session_id, session_epoch, "
    "lease_until, device_id, token_digest FROM customer_session_state"
)


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _write_event(
    conn: psycopg.Connection,
    *,
    event: str,
    user_id: str,
    activation_code_id: str | None,
    device_id: str,
    session_id: str,
    session_epoch: int,
    request_id: str,
    actor_user_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Append one row to the append-only session audit trail."""
    conn.execute(
        "INSERT INTO customer_session_events "
        "(id, event, user_id, activation_code_id, device_id, session_id, "
        " session_epoch, actor_user_id, reason, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            str(uuid.uuid4()),
            event,
            user_id,
            activation_code_id,
            device_id,
            session_id,
            session_epoch,
            actor_user_id,
            reason,
            request_id,
        ),
    )


# ---------------------------------------------------------------------------
# Login (dev doc §12.3)
# ---------------------------------------------------------------------------


def login_session(
    conn: psycopg.Connection,
    *,
    user_id: str,
    activation_code_id: str | None,
    device_id: str,
    presentation_session_token: str | None,
    request_id: str,
    now: datetime,
    takeover: bool = False,
) -> LoginResult:
    """Establish or renew a session for this device only.

    Different devices never replace one another. The legacy takeover parameter
    remains accepted for older clients, with no cross-device effect.
    """
    # SES-01: the caller must supply the PostgreSQL server clock — a missing
    # clock must fail closed instead of silently drifting to the host clock.
    now_full = now
    now_iso = now_full.replace(microsecond=0).isoformat()

    row = conn.execute(
        f"{_ROW_SQL} WHERE user_id = %s AND device_id = %s FOR UPDATE",
        (user_id, device_id),
    ).fetchone()
    if row is None:
        # Defensive branch: activation always inserts the row, but a lost row
        # must still yield a sound epoch-1 session instead of a hard failure.
        # Two concurrent first-writers race on the PK: ON CONFLICT DO NOTHING
        # (inside _establish) hands the loser the winner's committed row to
        # re-drive the state machine — never a UniqueViolation 500. No
        # production path deletes session rows, so a lost race always
        # re-reads a row; the retry bound is pure paranoia.
        for _attempt in range(3):
            established = _establish(
                conn,
                device_id=device_id,
                user_id=user_id,
                activation_code_id=activation_code_id,
                old_row=None,
                now=now_full,
                request_id=request_id,
            )
            if established is not None:
                return established
            row = conn.execute(
                f"{_ROW_SQL} WHERE user_id = %s AND device_id = %s FOR UPDATE",
                (user_id, device_id),
            ).fetchone()
            if row is not None:
                break
        else:
            raise RuntimeError(
                "customer_session_state row vanished during a defensive first-write race"
            )

    user_id = str(row[0])
    activation_code_id = str(row[1]) if row[1] is not None else None
    session_id = str(row[2])
    session_epoch = int(row[3])
    lease_until_raw = str(row[4])
    token_digest = str(row[6])
    lease_until = _as_utc(lease_until_raw)

    lapsed = now_full > lease_until

    presented_digests = (
        _token_digests(presentation_session_token) if presentation_session_token else []
    )
    token_matches = any(digest == token_digest for digest in presented_digests)

    if token_matches and not lapsed:
        # Renewal only: same token, same epoch, lease pushed out (§12.3).
        lease_until_iso = (
            (now_full + timedelta(seconds=SESSION_LEASE_SECONDS)).replace(microsecond=0).isoformat()
        )
        conn.execute(
            "UPDATE customer_session_state "
            "SET lease_until = %s, updated_at = %s WHERE session_id = %s",
            (lease_until_iso, now_iso, session_id),
        )
        _write_event(
            conn,
            event="LOGIN",
            user_id=user_id,
            activation_code_id=activation_code_id,
            device_id=device_id,
            session_id=session_id,
            session_epoch=session_epoch,
            request_id=request_id,
            actor_user_id=user_id,
        )
        return LoginResult(
            outcome=LOGIN_RENEWED,
            session_token=presentation_session_token,
            session_id=session_id,
            session_epoch=session_epoch,
            lease_until=lease_until_iso,
        )
    # Same device, unusable token (or lapsed): the recovery path — epoch
    # + 1, fresh token. A lapsed prior lease records the TIMEOUT event.
    return _require_established(
        _establish(
            conn,
            device_id=device_id,
            user_id=user_id,
            activation_code_id=activation_code_id,
            old_row=row,
            lapsed=lapsed,
            now=now_full,
            request_id=request_id,
        )
    )


def _require_established(result: LoginResult | None) -> LoginResult:
    """Narrow ``_establish``: only the defensive first-write path answers None."""
    if result is None:
        raise RuntimeError("unreachable: establish returned None with a locked row")
    return result


def _establish(
    conn: psycopg.Connection,
    *,
    device_id: str,
    user_id: str,
    activation_code_id: str | None,
    old_row: tuple[Any, ...] | None,
    now: datetime,
    request_id: str,
    lapsed: bool = False,
) -> LoginResult | None:
    """Establish (create or bump) the caller's session and audit it.

    ``old_row`` is the locked pre-existing row (``None`` = the defensive
    first-session insert). A lapsed prior lease appends the system ``TIMEOUT``
    event before the fresh ``LOGIN`` event. The defensive insert answers
    ``None`` when a concurrent first-writer won the PK — the caller re-reads
    the winner's row and re-drives the state machine (never a 500).
    """
    now_iso = now.replace(microsecond=0).isoformat()
    lease_until_iso = (
        (now + timedelta(seconds=SESSION_LEASE_SECONDS)).replace(microsecond=0).isoformat()
    )

    session_token = secrets.token_urlsafe(32)
    version, key = highest_device_domain_key()
    token_digest = keyed_digest(key, session_token)
    session_id = str(uuid.uuid4())

    if old_row is None:
        new_epoch = 1
        inserted = conn.execute(
            "INSERT INTO customer_session_state "
            "(user_id, activation_code_id, device_id, session_id, token_digest, "
            " session_epoch, lease_until) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (device_id) DO NOTHING",
            (
                user_id,
                activation_code_id,
                device_id,
                session_id,
                token_digest,
                new_epoch,
                lease_until_iso,
            ),
        )
        if inserted.rowcount == 0:
            # Lost the concurrent first-write race: the winner's committed
            # row exists — the caller re-reads it and re-drives the state
            # machine instead of dying on UniqueViolation.
            return None
    else:
        old_session_id = str(old_row[2])
        old_epoch = int(old_row[3])
        new_epoch = old_epoch + 1
        conn.execute(
            "UPDATE customer_session_state "
            "SET device_id = %s, session_id = %s, token_digest = %s, "
            "session_epoch = %s, lease_until = %s, last_heartbeat_at = NULL, "
            "updated_at = %s WHERE session_id = %s",
            (
                device_id,
                session_id,
                token_digest,
                new_epoch,
                lease_until_iso,
                now_iso,
                old_session_id,
            ),
        )
        if lapsed:
            # System-driven timeout of the prior lease: no acting user. The
            # event describes the *timed-out* session, so it carries the old
            # row's binding (device id, activation code, session, epoch).
            _write_event(
                conn,
                event="TIMEOUT",
                user_id=user_id,
                activation_code_id=str(old_row[1]) if old_row[1] is not None else None,
                device_id=str(old_row[5]),
                session_id=old_session_id,
                session_epoch=old_epoch,
                request_id=request_id,
            )

    _write_event(
        conn,
        event="LOGIN",
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=new_epoch,
        request_id=request_id,
        actor_user_id=user_id,
    )
    return LoginResult(
        outcome=LOGIN_CREATED,
        session_token=session_token,
        session_id=session_id,
        session_epoch=new_epoch,
        lease_until=lease_until_iso,
    )


# ---------------------------------------------------------------------------
# Heartbeat (dev doc §12.3 same-device lease extension)
# ---------------------------------------------------------------------------


def heartbeat_session(
    conn: psycopg.Connection,
    *,
    presentation_session_token: str,
    request_id: str,
    now: datetime,
) -> HeartbeatResult:
    """Renew the live session lease; refuse stale and lapsed tokens.

    The row is located by the presented token's digests (probed across every
    configured key version) and locked: either the token still owns the row
    or it does not. A matching row under a lapsed lease is terminal — the
    lease is never resurrected (acceptance §3.4).
    """
    now_full = now

    digests = _token_digests(presentation_session_token)
    row = conn.execute(
        f"{_ROW_SQL} WHERE token_digest = ANY(%s) LIMIT 1 FOR UPDATE",
        (digests,),
    ).fetchone()
    if row is None:
        # The token never resolved to the live row: replaced (or forged — one
        # answer, no oracle).
        return HeartbeatResult(outcome=HEARTBEAT_REPLACED)

    user_id = str(row[0])
    activation_code_id = str(row[1]) if row[1] is not None else None
    session_id = str(row[2])
    session_epoch = int(row[3])
    lease_until_raw = str(row[4])
    device_id = str(row[5])

    if now_full > _as_utc(lease_until_raw):
        # Lapsed: terminal. No event, no resurrection.
        return HeartbeatResult(
            outcome=HEARTBEAT_EXPIRED,
            session_id=session_id,
            session_epoch=session_epoch,
        )

    lease_until_iso = (
        (now_full + timedelta(seconds=SESSION_LEASE_SECONDS)).replace(microsecond=0).isoformat()
    )
    # PR #47 Codex review P2 (the unbind lesson): keep the PostgreSQL
    # microsecond precision — a whole-second trim lands a same-second
    # heartbeat before created_at and trips the heartbeat_not_before_created
    # CHECK.
    now_full_iso = now_full.isoformat()
    conn.execute(
        "UPDATE customer_session_state "
        "SET lease_until = %s, last_heartbeat_at = %s, updated_at = %s "
        "WHERE session_id = %s",
        (lease_until_iso, now_full_iso, now_full_iso, session_id),
    )
    _write_event(
        conn,
        event="HEARTBEAT",
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=session_epoch,
        request_id=request_id,
        actor_user_id=user_id,
    )
    return HeartbeatResult(
        outcome=HEARTBEAT_OK,
        session_id=session_id,
        session_epoch=session_epoch,
        lease_until=lease_until_iso,
    )


# ---------------------------------------------------------------------------
# Logout (dev doc §3.3 / §12.3)
# ---------------------------------------------------------------------------


def logout_session(
    conn: psycopg.Connection,
    *,
    presentation_session_token: str,
    request_id: str,
    now: datetime,
) -> LogoutResult:
    """Release the single-online slot by pulling the lease into the past.

    Mirrors the T16 unbind pattern: the lease lands in the past with the full
    transaction-clock precision (the PR #47 Codex review P2 lesson — trimming
    to whole seconds once left a released lease alive up to a second), the
    GREATEST backstop keeps the ``lease_after_created`` check satisfied, a
    ``LOGOUT`` event with reason ``user_logout`` lands on the audit trail,
    and the other device may log in immediately. A token that no longer owns
    the row answers ``replaced`` without touching the new session; a lapsed
    lease answers ``expired`` (nothing to release).
    """
    now_full = now

    digests = _token_digests(presentation_session_token)
    row = conn.execute(
        f"{_ROW_SQL} WHERE token_digest = ANY(%s) LIMIT 1 FOR UPDATE",
        (digests,),
    ).fetchone()
    if row is None:
        return LogoutResult(outcome=LOGOUT_REPLACED)

    user_id = str(row[0])
    activation_code_id = str(row[1]) if row[1] is not None else None
    session_id = str(row[2])
    session_epoch = int(row[3])
    lease_until_raw = str(row[4])
    device_id = str(row[5])

    if now_full > _as_utc(lease_until_raw):
        return LogoutResult(
            outcome=LOGOUT_EXPIRED,
            session_id=session_id,
            session_epoch=session_epoch,
        )

    now_full_iso = now_full.isoformat()
    conn.execute(
        "UPDATE customer_session_state "
        "SET lease_until = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s WHERE session_id = %s",
        (now_full_iso, now_full_iso, session_id),
    )
    _write_event(
        conn,
        event="LOGOUT",
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        session_id=session_id,
        session_epoch=session_epoch,
        request_id=request_id,
        actor_user_id=user_id,
        reason=LOGOUT_REASON,
    )
    return LogoutResult(
        outcome=LOGOUT_OK,
        session_id=session_id,
        session_epoch=session_epoch,
    )


# ---------------------------------------------------------------------------
# Explicit switch (dev doc §12.3 fifth line)
# ---------------------------------------------------------------------------


def switch_session(
    conn: psycopg.Connection,
    *,
    user_id: str,
    activation_code_id: str | None,
    device_id: str,
    presentation_session_token: str | None,
    request_id: str,
    now: datetime,
) -> LoginResult:
    """Compatibility entry point: log in this device without displacing others."""
    return login_session(
        conn,
        user_id=user_id,
        activation_code_id=activation_code_id,
        device_id=device_id,
        presentation_session_token=presentation_session_token,
        request_id=request_id,
        now=now,
        takeover=True,
    )


# ---------------------------------------------------------------------------
# Revocation propagation (T20 / SES-03)
# ---------------------------------------------------------------------------


# The session-event reasons recorded by the admin-driven revocations.
REASON_CODE_SUSPENDED = "code_suspended"
REASON_CODE_REVOKED = "code_revoked"

# The reasons recorded when a master reevaluates one of its sub-accounts
# (customer self-service lane): deactivation and credential rotation both
# terminate the sub's live session in the same transaction.
REASON_SUB_ACCOUNT_DEACTIVATED = "sub_account_deactivated"
REASON_SUB_ACCOUNT_PASSWORD_RESET = "sub_account_password_reset"

# The session/device rows that must be purged before a customer row can be
# physically deleted: their ``user_id`` FKs carry no CASCADE. Order matters —
# the session state references the device, so it goes first. The session-event
# log is deliberately absent: ``customer_session_events`` is append-only (029 —
# the database refuses UPDATE/DELETE outright), so a session history is kept
# and pins the account the same way business history does — the DELETE
# degrades (customer lane) or answers the has-history conflict (admin lane)
# instead of rewriting the audit. Business history (ledger, tasks) is absent
# for the same reason.
SUB_ACCOUNT_FOOTPRINT_TABLES = (
    "customer_session_state",
    "customer_devices",
)


def revoke_session(
    conn: psycopg.Connection,
    *,
    user_id: str,
    actor_user_id: str,
    reason: str,
    request_id: str,
    now_iso: str,
    device_id: str | None = None,
) -> bool:
    """Terminate the user's live session atomically (SES-03 propagation).

    Epoch bump + lease pulled into the past + a ``LOGOUT`` event naming the
    acting user and the reason — one transaction, exactly the T16 unbind
    core generalized. The GREATEST backstop keeps the
    ``lease_after_created`` CHECK satisfied; one microsecond — not one
    second — preserves the "immediately expired" semantics (the PR #47
    Codex review P2 lesson). ``device_id`` scopes the revocation to the
    session riding that device (the T16/T18 unbind/revoke delegation);
    ``None`` revokes whatever session the user holds (the admin code
    suspend/revoke paths — a suspended, revoked or otherwise disabled
    account loses its session in the same transaction).

    Answers ``True`` when a session row was actually revoked; a user with
    no session row (never activated, or the row was lost) is a no-op —
    there was nothing to propagate.
    """
    where_clause = "WHERE user_id = %s"
    params: list[object] = [user_id]
    if device_id is not None:
        where_clause += " AND device_id = %s"
        params.append(device_id)
    session_rows = conn.execute(
        "UPDATE customer_session_state "
        "SET session_epoch = session_epoch + 1, "
        "lease_until = GREATEST(%s::timestamptz, "
        "created_at::timestamptz + interval '1 microsecond'), "
        "updated_at = %s "
        f"{where_clause} "
        "RETURNING activation_code_id, device_id, session_id, session_epoch",
        (now_iso, now_iso, *params),
    ).fetchall()
    for session_row in session_rows:
        _write_event(
            conn,
            event="LOGOUT",
            user_id=user_id,
            activation_code_id=str(session_row[0]) if session_row[0] is not None else None,
            device_id=str(session_row[1]),
            session_id=str(session_row[2]),
            session_epoch=int(session_row[3]),
            request_id=request_id,
            actor_user_id=actor_user_id,
            reason=reason,
        )
    return bool(session_rows)
