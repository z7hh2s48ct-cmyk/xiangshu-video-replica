"""Shared admin write contract + idempotency snapshot layer (revision 031).

Extracted by the 2026-09-02 admin-console assessment (A6): the contract
envelope, request fingerprinting, and the snapshot layer existed as two full
copies (``admin_activation_routes`` and ``admin_customer_routes``), while the
device lane imported the activation copy's private symbols. One module now
owns the semantics:

- **Write contract** (dev doc §15): every mutation sends a non-blank
  ``Idempotency-Key`` header, ``confirm: true`` and a non-blank ``reason``.
- **Fingerprint**: the canonical route *template* plus the concrete path
  params and body — the same key against a different resource answers 409.
- **Snapshot**: the response payload (never plaintext codes) is persisted
  before commit so an ambiguous retry replays the original outcome.
- **Deferred errors**: ``DeferredHTTPWriteError`` snapshots an error outcome
  whose side effects must survive it (the lazy EXPIRED flip, the audited
  self-service denial).

Domains pass their own ``unavailable_code``/``unavailable_message`` so the
client error tables stay unambiguous (§13.2).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import psycopg
from fastapi import HTTPException, Request, Response
from pydantic import BaseModel

from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.ops_metrics import get_or_create_request_id, set_current_result_code

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REQUEST_ID_HEADER = "X-Request-Id"
REPLAY_HEADER = "X-Idempotent-Replay"


class AdminWriteContract(BaseModel):
    """Shared write-contract fields for every admin mutation."""

    confirm: bool = False
    reason: str = ""


class AdminWriteActor(Protocol):
    """Minimal actor shape required by the idempotency envelope."""

    @property
    def user_id(self) -> str: ...


def http_error(status: int, code: str, message: str) -> HTTPException:
    """The admin-domain error envelope: result-code metric + JSON detail."""
    set_current_result_code(code)
    return HTTPException(status_code=status, detail={"code": code, "message": message})


def transaction_now_iso(conn: psycopg.Connection) -> str:
    """The trusted PostgreSQL clock on the caller's transaction (SES-01).

    Judgements recorded on status columns belong to the same server-side
    clock the session routes use, never the possibly skewed application
    clock.
    """
    row = conn.execute("SELECT now()").fetchone()
    now = row[0] if row is not None else datetime.now(UTC)
    return now.isoformat()


def require_write_contract(request: Request, body: AdminWriteContract) -> tuple[str, str]:
    """Validate the write contract; returns (idempotency_key, reason)."""
    key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not key:
        raise http_error(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
    if not body.confirm:
        raise http_error(400, "CONFIRMATION_REQUIRED", "This write requires confirm=true.")
    reason = body.reason.strip()
    if not reason:
        raise http_error(400, "REASON_REQUIRED", "A non-blank reason is required.")
    return key, reason


def canonical_route(request: Request) -> str:
    """``METHOD /route/template`` — the route, never the concrete path."""
    route = request.scope.get("route")
    template = getattr(route, "path", request.url.path)
    return f"{request.method.upper()} {template}"


def request_hash(route: str, path_params: Mapping[str, str], body: BaseModel) -> str:
    """Freeze the canonical request for conflict checks.

    The route *template* alone does not identify the target resource: the same
    key with the same body against ``/activation-codes/{code_id}/revoke`` for
    code A and code B would otherwise hash identically, so the second call
    would wrongly replay the first response while B stays untouched (PR #43
    review P2). The concrete path parameters are therefore part of the
    fingerprint.
    """
    payload = json.dumps(
        {
            "route": route,
            "path_params": {name: path_params[name] for name in sorted(path_params)},
            "body": body.model_dump(mode="json"),
        },
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencySnapshot:
    request_hash: str
    response_status: int | None
    response_body: str | None


def begin_idempotent_write(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    route: str,
    idempotency_key: str,
    request_hash: str,
) -> str | None:
    """Insert the placeholder row; returns its id, or ``None`` on key reuse."""
    row_id = str(uuid.uuid4())
    inserted = conn.execute(
        "INSERT INTO admin_write_idempotency "
        "(id, actor_user_id, route, idempotency_key_digest, request_hash) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (actor_user_id, route, idempotency_key_digest) DO NOTHING",
        (
            row_id,
            actor_user_id,
            route,
            idempotency_key_digest(idempotency_key),
            request_hash,
        ),
    ).rowcount
    return row_id if inserted == 1 else None


def load_idempotent_snapshot(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    route: str,
    idempotency_key: str,
) -> IdempotencySnapshot | None:
    row = conn.execute(
        "SELECT request_hash, response_status, response_body "
        "FROM admin_write_idempotency "
        "WHERE actor_user_id = %s AND route = %s AND idempotency_key_digest = %s",
        (actor_user_id, route, idempotency_key_digest(idempotency_key)),
    ).fetchone()
    if row is None:
        return None
    # A committed placeholder whose response never landed (malformed envelope
    # state) must answer 409 on key reuse — never a TypeError-turned-500.
    return IdempotencySnapshot(
        request_hash=str(row[0]),
        response_status=None if row[1] is None else int(row[1]),
        response_body=None if row[2] is None else str(row[2]),
    )


def finish_idempotent_write(
    conn: psycopg.Connection,
    placeholder_id: str,
    *,
    response_status: int,
    response_body: dict[str, object],
) -> None:
    conn.execute(
        "UPDATE admin_write_idempotency SET response_status = %s, response_body = %s WHERE id = %s",
        (
            response_status,
            json.dumps(response_body, ensure_ascii=False, separators=(",", ":")),
            placeholder_id,
        ),
    )


class DeferredHTTPWriteError(Exception):
    """A deterministic error outcome whose side effects must survive it.

    The T18 admin pairing approval hit a shape the T12 lanes never had: the
    ``expired`` outcome *writes* (the lazy PENDING/APPROVED → EXPIRED flip,
    the T17 customer-lane semantic) before answering 409. A plain
    ``HTTPException`` raised inside ``business`` would roll the transaction
    back and silently drop that flip. Raising this subclass instead tells
    ``write_with_idempotency`` to snapshot the error response, commit the
    side effects and re-raise the ``HTTPException`` *after* the commit — so
    the replay of the same idempotency key returns the same error, and the
    lazy flip survives exactly like the T17 route's raise-outside-the-``with``
    pattern. Branches with no side effects keep raising ``HTTPException``
    directly (rollback, the key stays free for a retry — the T12 precedent).
    """

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(code)
        set_current_result_code(code)
        self.status_code = status_code
        self.body: dict[str, object] = {"detail": {"code": code, "message": message}}


def write_with_idempotency(
    request: Request,
    response: Response,
    actor: AdminWriteActor,
    body: AdminWriteContract,
    business: Callable[[psycopg.Connection, str], dict[str, object]],
    *,
    success_status: int,
    unavailable_code: str = "ACTIVATION_SERVICE_UNAVAILABLE",
    unavailable_message: str = "Activation code management requires the PostgreSQL runtime.",
) -> dict[str, object]:
    """Run one admin write behind the idempotency snapshot layer.

    ``business`` receives the transaction connection and the request id and
    returns the response payload; the payload is snapshotted before commit.
    Callers keep plaintext codes out of it (No-Go red line) — the download
    path bypasses this layer precisely because its response must not persist.

    The 503 fail-closed code/message defaults to the activation lane; each
    domain passes its own so the §13.2 client tables stay unambiguous.
    """
    idempotency_key, _reason = require_write_contract(request, body)
    route = canonical_route(request)
    fingerprint = request_hash(route, dict(request.path_params), body)
    request_id = get_or_create_request_id(request)
    try:
        with pg_transaction() as conn:
            placeholder = begin_idempotent_write(
                conn,
                actor_user_id=actor.user_id,
                route=route,
                idempotency_key=idempotency_key,
                request_hash=fingerprint,
            )
            if placeholder is None:
                snapshot = load_idempotent_snapshot(
                    conn,
                    actor_user_id=actor.user_id,
                    route=route,
                    idempotency_key=idempotency_key,
                )
                if (
                    snapshot is None
                    or snapshot.request_hash != fingerprint
                    or snapshot.response_status is None
                    or snapshot.response_body is None
                ):
                    raise http_error(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key was already used for a different request.",
                    )
                replayed: dict[str, object] = json.loads(snapshot.response_body)
                response.status_code = snapshot.response_status
                response.headers[REPLAY_HEADER] = "true"
                # Older daily-price writes stored a bare JSON list. Replaying
                # those must not call dict methods on that historical snapshot.
                replay_request_id = (
                    replayed.get("request_id") if isinstance(replayed, dict) else None
                )
                if isinstance(replay_request_id, str):
                    response.headers[REQUEST_ID_HEADER] = replay_request_id
                return replayed
            deferred: DeferredHTTPWriteError | None = None
            try:
                payload = business(conn, request_id)
            except DeferredHTTPWriteError as exc:
                # Snapshot the error response and keep the transaction — the
                # business side effects (the lazy EXPIRED flip) must survive
                # the 409, and the replay must answer the same error.
                deferred = exc
                payload = exc.body
            finish_idempotent_write(
                conn,
                placeholder,
                response_status=deferred.status_code if deferred is not None else success_status,
                response_body=payload,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            deferred_error = deferred
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        # The PG runtime is unavailable (internal SQLite deployments) or the
        # idempotency envelope state is malformed: fail closed. A bare
        # ValueError deliberately does NOT map to 503 — MissingDatabaseConfigError
        # is the only expected ValueError subclass here; any other ValueError is
        # a real bug and surfaces as a 500 (assessment A3).
        raise http_error(503, unavailable_code, unavailable_message) from exc
    if deferred_error is not None:
        # Re-raised only after the commit: the HTTPException handler builds a
        # fresh response, so the request-id header set above does not ride it
        # (the plain-raise error paths behave the same way).
        raise HTTPException(
            status_code=deferred_error.status_code,
            detail=deferred_error.body["detail"],
        ) from deferred_error
    return payload
