"""T19 / SES-01 + T20 / SES-02/SES-03 — session routes: login, switch,
heartbeat, logout.

Routes (dev doc §6.1):

- ``POST /api/customer/sessions/login`` — device-credential login; returns a
  new session token on establishment/recovery, renews on same-device
  presentation of the valid session token, answers 409 ``OTHER_DEVICE_ONLINE``
  when the other device holds a live lease.
- ``POST /api/customer/sessions/switch`` — the explicit atomic switch (T20,
  dev doc §12.3 fifth line): displaces a live lease on the other device in
  one transaction (SWITCH event + epoch bump + fresh token) so the old
  token is fenced out on every API instance the moment it commits. Every
  other branch matches the login semantics (renewal, recovery, timeout
  takeover) — a switch never invents new states, and never silently kicks
  without the explicit client confirmation flow (SES-02 No-Go).
- ``POST /api/customer/sessions/heartbeat`` — session-token renewal of the lease.
- ``POST /api/customer/sessions/logout`` — session-token logout; pulls the
  lease into the past and appends the LOGOUT event.

Authentication layers:

- login/switch: the *device credential* (``Authorization: Bearer
  <device-token>``, the T16 layer) plus the *code-status gate* (T20/SES-03:
  only an ACTIVE activation code may establish a session — a suspended or
  revoked code answers 403 and the client must not reach the workspace);
- heartbeat/logout: the *session token* (``Authorization: Bearer <session-token>``).

Idempotency (dev doc §6.3): login, switch and logout carry a mandatory
``Idempotency-Key``. The sealed envelope replays the lost response (same
token, same epoch, no second LOGIN event). Same key against a different
request body answers 409 ``IDEMPOTENCY_CONFLICT``. Heartbeat is naturally
idempotent (renewal) and carries no envelope.

Rate limiting (T15/T45 infrastructure): login and switch both draw a
per-device credential budget. The shared ``login:ip`` bucket remains an
auxiliary barrier for unknown credentials, while a known credential is not
locked out merely because other customers share its NAT address. Heartbeat
and logout use a separate pre-authentication IP budget. Spent budgets answer
429 ``RATE_LIMITED`` with ``Retry-After``.

Stable error codes (dev doc §13.2 plus the T16 precedent for REQUIRED /
INVALID variants):

- 401 ``DEVICE_CREDENTIAL_REQUIRED`` / ``DEVICE_CREDENTIAL_INVALID`` /
  ``DEVICE_REVOKED`` (login/switch authentication);
- 403 ``CODE_SUSPENDED`` / ``CODE_REVOKED`` (T20/SES-03: the code-status
  gate — a suspended or revoked account never establishes a session);
- 401 ``SESSION_TOKEN_REQUIRED`` (heartbeat/logout missing the Bearer token);
- 401 ``SESSION_REPLACED`` — the presented token no longer owns the live
  session (another device took over, or the token is unknown);
- 401 ``SESSION_EXPIRED`` — the token matched but the lease has lapsed;
  the session is never resurrected;
- 409 ``OTHER_DEVICE_ONLINE`` — login conflict, masked device hint + the
  remaining lease (never a silent kick, dev doc §3.3);
- 400 ``IDEMPOTENCY_KEY_REQUIRED`` / 409 ``IDEMPOTENCY_CONFLICT``;
- 429 ``RATE_LIMITED``;
- 503 ``SESSION_SERVICE_UNAVAILABLE`` — PG runtime / key misconfiguration.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict

from app.activation_code_service import ActivationKeyError
from app.api_errors import http_error as _http
from app.auth_headers import bearer_token as _bearer_token
from app.customer_auth import SessionFencingError, verify_session_context
from app.customer_device_service import (
    AuthenticatedDevice,
    DeviceCredentialLookup,
    _token_digests,
    lookup_device_credential,
)
from app.customer_idempotency import (
    EnvelopeRecord,
    IdempotencyKeyError,
    complete_envelope,
    customer_aead_key,
    envelope_aad,
    highest_customer_aead_key,
    idempotency_key_digests,
    insert_envelope,
    load_envelope,
    open_response,
    recovery_window_seconds,
    request_hash,
    seal_response,
)
from app.customer_session_service import (
    HEARTBEAT_EXPIRED,
    HEARTBEAT_REPLACED,
    LOGIN_CONFLICT,
    LOGIN_CREATED,
    LOGIN_RENEWED,
    LOGOUT_EXPIRED,
    LOGOUT_REPLACED,
    heartbeat_session,
    login_session,
    logout_session,
    switch_session,
)
from app.db_pg import get_pg_pool, pg_transaction
from app.ops_metrics import get_or_create_request_id
from app.security_rate_limit import (
    DIMENSION_CUSTOMER_PREAUTH_IP,
    DIMENSION_LOGIN_ACCOUNT,
    DIMENSION_LOGIN_IP,
    client_ip_from_request,
    consume_rate_limit,
    customer_preauth_ip_limit,
    login_account_limit,
    login_ip_limit,
    rate_limit_window_seconds,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
RETRY_AFTER_HEADER = "Retry-After"

LOGIN_OPERATION = "session_login"
SWITCH_OPERATION = "session_switch"
LOGOUT_OPERATION = "session_logout"

# The sealed login payload carries the state-machine outcome so a replay can
# reproduce the original status code (200 renewed / 201 established).
OUTCOME_FIELD = "_outcome"

router = APIRouter(prefix="/api/customer/sessions", tags=["customer-sessions"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_token: str | None = None


class LoginResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    device_id: str
    session_id: str
    session_token: str
    session_epoch: int
    session_lease_expires_at: str
    request_id: str


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    session_epoch: int
    lease_expires_at: str
    request_id: str


# T28 (PR #55 review): the same device renewing its live session answers 200
# with the identical sealed LoginResponse body, so both status codes must
# carry the model in the contract — a 201-only declaration leaves generated
# clients unable to type a valid production response.
RENEWAL_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": LoginResponse,
        "description": "The same device renewed its live session "
        "(the outcome-sealed envelope replays with the original token).",
    },
}


def _request_id(request: Request) -> str:
    return get_or_create_request_id(request)


def _require_pg() -> None:
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Customer sessions require the PostgreSQL runtime.",
        ) from exc


def _consume_customer_preauth(request: Request) -> None:
    with pg_transaction() as conn:
        decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_CUSTOMER_PREAUTH_IP,
            identifier=client_ip_from_request(request),
            limit=customer_preauth_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    if not decision.allowed:
        blocked = _http(429, "RATE_LIMITED", "Too many customer session requests.")
        blocked.headers = {RETRY_AFTER_HEADER: str(decision.retry_after_seconds)}
        raise blocked


def _transaction_now(conn: psycopg.Connection) -> datetime:
    """SES-01: PostgreSQL is the only trusted clock — sample it inside the
    business transaction (the unbind/activation precedents) so the lease
    judgement and every written timestamp share one server-side clock
    instead of a possibly skewed application clock."""
    now_row = conn.execute("SELECT now()").fetchone()
    now: datetime = now_row[0] if now_row is not None else datetime.now(UTC)
    return now


# ---------------------------------------------------------------------------
# Envelope replay helpers (the T16 unbind-envelope precedent)
# ---------------------------------------------------------------------------


def _find_envelope(
    conn: psycopg.Connection,
    *,
    operation: str,
    scopes: list[str],
    key_digests: list[str],
) -> tuple[str, str, EnvelopeRecord, datetime] | None:
    """The committed envelope for this key across the scope candidates.

    Returns the PostgreSQL ``now()`` sampled in this same envelope-read
    transaction: the recovery-window verdict must use the trusted
    server-side clock, never the application process clock (SES-01, the
    activation-route ``_server_now`` precedent; PR #51 review P2)."""
    for scope in scopes:
        for key_digest in key_digests:
            record = load_envelope(
                conn,
                operation=operation,
                scope=scope,
                key_digest=key_digest,
            )
            if record is not None:
                return scope, key_digest, record, _transaction_now(conn)
    return None


def _envelope_recoverable(
    record: EnvelopeRecord, *, req_hash: str, now: datetime
) -> tuple[str, int]:
    """Validate the envelope for replay; returns (ciphertext, key_version).

    Raises 409 ``IDEMPOTENCY_CONFLICT`` unless the envelope replays this
    exact request; the returned pair is fully narrowed for the AEAD open.
    The recovery window is judged against ``now`` — the PostgreSQL clock
    sampled in the envelope-read transaction (never the process clock;
    PR #51 review P2)."""
    if record.request_hash != req_hash:
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different request.",
        )
    if (
        record.purged_at is not None
        or record.ciphertext is None
        or record.key_version is None
        or record.recovery_expires_at is None
        or datetime.fromisoformat(str(record.recovery_expires_at)) <= now
    ):
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key is no longer recoverable.",
        )
    return str(record.ciphertext), int(record.key_version)


def _replay_login_response(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    scope: str,
    key_digest: str,
    now: datetime,
    operation: str,
) -> tuple[LoginResponse, int]:
    """Replay the sealed login/switch response (and its original status code)."""
    ciphertext, key_version = _envelope_recoverable(record, req_hash=req_hash, now=now)
    try:
        sealed = open_response(
            ciphertext,
            key=customer_aead_key(key_version),
            aad=envelope_aad(operation, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "The sealed login response could not be decrypted.",
        ) from None
    outcome = str(sealed.pop(OUTCOME_FIELD, LOGIN_CREATED))
    status_code = 200 if outcome == LOGIN_RENEWED else 201
    return LoginResponse.model_validate(sealed), status_code


def _replay_validated_login_response(
    conn: psycopg.Connection,
    record: EnvelopeRecord,
    *,
    req_hash: str,
    scope: str,
    key_digest: str,
    now: datetime,
    operation: str,
    device_token: str,
) -> tuple[LoginResponse, int]:
    """Open a sealed response only if its credential and live row still agree."""
    replayed, status_code = _replay_login_response(
        record,
        req_hash=req_hash,
        scope=scope,
        key_digest=key_digest,
        now=now,
        operation=operation,
    )
    try:
        lookup = lookup_device_credential(conn, device_token)
    except ActivationKeyError:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Device credential keys are not configured.",
        ) from None
    if lookup.device is None:
        if lookup.row_status is not None:
            raise _http(401, "DEVICE_REVOKED", "This device credential has been revoked.")
        raise _http(401, "DEVICE_CREDENTIAL_INVALID", "The device credential is invalid.")
    device = lookup.device
    _require_device_authority(conn, device, replayed.session_token)
    try:
        session_digests = _token_digests(replayed.session_token)
    except ActivationKeyError:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Session keys are not configured.",
        ) from None
    row = conn.execute(
        """
        SELECT user_id, activation_code_id, device_id, session_id,
               session_epoch, lease_until
        FROM customer_session_state
        WHERE token_digest = ANY(%s)
        LIMIT 1
        FOR UPDATE
        """,
        (session_digests,),
    ).fetchone()
    lease_until = None if row is None else datetime.fromisoformat(str(row[5]))
    if lease_until is not None and lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=UTC)
    if (
        row is None
        or str(row[0]) != replayed.user_id
        or str(row[0]) != device.user_id
        or (str(row[1]) if row[1] is not None else None) != device.activation_code_id
        or str(row[2]) != replayed.device_id
        or str(row[2]) != device.id
        or str(row[3]) != replayed.session_id
        or int(row[4]) != replayed.session_epoch
        or lease_until is None
        or lease_until <= now
    ):
        raise _http(
            409,
            "SESSION_REPLAY_STALE",
            "The sealed session has been replaced or expired.",
        )
    return replayed, status_code


def _replay_logout_response(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    scope: str,
    key_digest: str,
    now: datetime,
) -> None:
    """Replay the sealed logout (a 204 with no body — integrity-checked)."""
    ciphertext, key_version = _envelope_recoverable(record, req_hash=req_hash, now=now)
    try:
        open_response(
            ciphertext,
            key=customer_aead_key(key_version),
            aad=envelope_aad(LOGOUT_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "The sealed logout response could not be decrypted.",
        ) from None


def _recovery_expires_at(conn: psycopg.Connection) -> str:
    """The recovery-window deadline on the PostgreSQL clock (the trusted one)."""
    now_row = conn.execute("SELECT now()").fetchone()
    now = now_row[0] if now_row is not None else datetime.now(UTC)
    return (now + timedelta(seconds=recovery_window_seconds())).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# POST /login + POST /switch — the §12.3 state-machine routes (shared core)
# ---------------------------------------------------------------------------


def _require_device_authority(
    conn: psycopg.Connection,
    device: AuthenticatedDevice,
    session_token: str | None,
) -> None:
    if device.activation_code_id is not None:
        _require_active_code(conn, device.activation_code_id)
        return
    # Password accounts may restore a live session, but a cached device credential
    # alone cannot log back in after logout/expiry and bypass the password gate.
    if not session_token:
        raise _http(401, "PASSWORD_LOGIN_REQUIRED", "请使用账号密码登录。")
    try:
        context = verify_session_context(conn, presentation_session_token=session_token)
    except SessionFencingError as exc:
        raise _http(401, "PASSWORD_LOGIN_REQUIRED", "请使用账号密码重新登录。") from exc
    if context.user_id != device.user_id or context.device_id != device.id:
        raise _http(401, "PASSWORD_LOGIN_REQUIRED", "请使用账号密码重新登录。")


def _require_active_code(conn: psycopg.Connection, activation_code_id: str) -> None:
    """T20 / SES-03 code-status gate: only an ACTIVE code may hold a session.

    The device credential alone proves the *device*; the binding's authority
    is the activation code, so a suspended or revoked account must never
    establish (or re-establish) a session — the client sees the 403 and is
    steered away from the workspace instead of succeeding here and failing
    later on every fenced write.

    The code row is locked ``FOR UPDATE`` through establishment (PR #52 P1):
    this runs inside the same business transaction as ``login_session``, so
    the row lock is held until the session commits. An administrator
    suspend/revoke (which locks the code row first, then revokes the riding
    session) therefore serializes against establishment — either the gate
    reads ACTIVE and the session commits before the suspend takes effect
    (and the suspend then revokes it), or the suspend wins and the gate
    reads SUSPENDED and answers 403. A suspended/revoked code can never end
    up holding a live session.
    """
    row = conn.execute(
        "SELECT status FROM activation_codes WHERE id = %s FOR UPDATE",
        (activation_code_id,),
    ).fetchone()
    if row is None:
        # The device row's FK guarantees the code exists; a missing row means
        # the runtime state is broken — fail closed as a server-side outage.
        logger.warning("session code-status gate found no activation code row")
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "The activation code state could not be verified.",
        )
    status = str(row[0])
    if status == "ACTIVE":
        return
    if status == "SUSPENDED":
        raise _http(403, "CODE_SUSPENDED", "This activation code is suspended.")
    raise _http(403, "CODE_REVOKED", "This activation code has been revoked.")


def _establish_session_route(
    *,
    body: LoginRequest,
    request: Request,
    response: Response,
    operation: str,
    takeover: bool,
) -> LoginResponse:
    """Drive the §12.3 state machine for one authenticated device.

    ``login`` and ``switch`` share every layer — device-credential auth, the
    code-status gate, the idempotency envelope, the per-device plus auxiliary
    IP budgets, and the sealed response. The only difference is the ``takeover``
    flag handed to the state machine, which turns a live-other-device
    conflict into the explicit atomic switch (§12.3 fifth line) instead of
    the 409. The envelope ``operation`` keeps the two routes' sealed
    responses in disjoint namespaces.
    """
    _require_pg()

    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")

    device_token = _bearer_token(request)
    if device_token is None:
        raise _http(401, "DEVICE_CREDENTIAL_REQUIRED", "A Bearer device credential is required.")

    request_id = _request_id(request)
    # Request hash over the normalized body (session_token is the only field;
    # its sha256 stands in so the raw secret never reaches the hash).
    req_hash = request_hash(
        {
            "session_token_sha256": hashlib.sha256(
                (body.session_token or "").encode("utf-8")
            ).hexdigest(),
        }
    )

    # Envelope keys + scope (fail closed with 503 when *either* key family is
    # misconfigured — a server-side outage must never masquerade as a client
    # credential problem; the activation-route precedent). The scope is the
    # device token digest, probed across all configured key versions (the
    # enroll precedent).
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
        key_digests = idempotency_key_digests(idempotency_key)
        key_digest = key_digests[0]
        scope_candidates = list(reversed(_token_digests(device_token)))
    except (ActivationKeyError, IdempotencyKeyError):
        logger.warning("session keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Session keys are not configured; customer sessions are refused.",
        ) from None

    # Replay probe *before* the rate limiter (the activation-route T15 review
    # P2 rule): the sealed 201 is the proof of the completed submission, and
    # a legitimate retry — the client lost the response of an
    # already-successful login/switch — must replay without spending any
    # login:ip budget; charging retries would lock a legal user out of their
    # own cached response after a few network retries. The probe is read-only
    # and scoped to the presented credential's digest.
    with pg_transaction() as conn:
        found = _find_envelope(
            conn,
            operation=operation,
            scopes=scope_candidates,
            key_digests=key_digests,
        )
        if found is not None:
            scope, matched_digest, existing, envelope_now = found
            replayed, replay_status = _replay_validated_login_response(
                conn,
                existing,
                req_hash=req_hash,
                scope=scope,
                key_digest=matched_digest,
                now=envelope_now,
                operation=operation,
                device_token=device_token,
            )
            response.headers[REPLAY_HEADER] = "true"
            response.status_code = replay_status
            return replayed

    # IP-dimension rate limit (T15 shared counters, a separate transaction —
    # the spent budget is never refunded). Login and switch draw the *same*
    # login:ip budget: a switch is a login-shaped attempt, and the limiter
    # must not be bypassable by switching instead.
    client_ip = client_ip_from_request(request)
    with pg_transaction() as conn:
        ip_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_IP,
            identifier=client_ip,
            limit=login_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
        account_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_ACCOUNT,
            identifier=scope_candidates[0],
            limit=login_account_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
        known_device = True
        if not ip_decision.allowed:
            try:
                known_device = lookup_device_credential(conn, device_token).device is not None
            except ActivationKeyError:
                known_device = False
    if not account_decision.allowed or (not ip_decision.allowed and not known_device):
        retry_after = max(
            ip_decision.retry_after_seconds,
            account_decision.retry_after_seconds,
        )
        blocked = _http(429, "RATE_LIMITED", "Too many login attempts from this address.")
        blocked.headers = {RETRY_AFTER_HEADER: str(retry_after)}
        raise blocked

    # The business transaction: authenticate, gate the code status, take the
    # envelope, drive the state machine, seal the response — one commit.
    with pg_transaction() as conn:
        try:
            lookup: DeviceCredentialLookup = lookup_device_credential(conn, device_token)
        except ActivationKeyError:
            logger.warning("device keys unavailable: configuration is incomplete")
            raise _http(
                503,
                "SESSION_SERVICE_UNAVAILABLE",
                "Device credential keys are not configured.",
            ) from None
        if lookup.device is None:
            if lookup.row_status is not None:
                raise _http(401, "DEVICE_REVOKED", "This device credential has been revoked.")
            raise _http(401, "DEVICE_CREDENTIAL_INVALID", "The device credential is invalid.")
        device = lookup.device

        # T20 / SES-03: the code-status gate — a suspended or revoked code
        # never establishes (or re-establishes) a session. Inside the
        # business transaction, so the refused attempt rolls the envelope
        # back with it and the key stays reusable after a resume.
        _require_device_authority(conn, device, body.session_token)

        envelope_id = insert_envelope(
            conn,
            operation=operation,
            scope=scope_candidates[0],
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            # A concurrent same-key writer committed first (the insert blocked
            # on the unique index until the other transaction committed):
            # answer from the winner's envelope. This transaction holds no
            # writes of its own, so returning while the block unwinds is safe.
            found = _find_envelope(
                conn,
                operation=operation,
                scopes=scope_candidates,
                key_digests=key_digests,
            )
            if found is None:
                raise _http(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The sealed response is no longer recoverable.",
                )
            scope, matched_digest, existing, envelope_now = found
            replayed, replay_status = _replay_validated_login_response(
                conn,
                existing,
                req_hash=req_hash,
                scope=scope,
                key_digest=matched_digest,
                now=envelope_now,
                operation=operation,
                device_token=device_token,
            )
            response.headers[REPLAY_HEADER] = "true"
            response.status_code = replay_status
            return replayed

        # SES-01: PostgreSQL is the only trusted clock — sample it inside the
        # business transaction so the lease judgement and every written
        # timestamp share one server-side clock.
        now = _transaction_now(conn)
        if takeover:
            result = switch_session(
                conn,
                user_id=device.user_id,
                activation_code_id=device.activation_code_id,
                device_id=device.id,
                presentation_session_token=body.session_token,
                request_id=request_id,
                now=now,
            )
        else:
            result = login_session(
                conn,
                user_id=device.user_id,
                activation_code_id=device.activation_code_id,
                device_id=device.id,
                presentation_session_token=body.session_token,
                request_id=request_id,
                now=now,
            )
        if result.outcome == LOGIN_CONFLICT:
            # Business failure: rolls back with the transaction — the key
            # stays reusable once the lease actually lapses. (Unreachable on
            # the switch route — takeover displaces the lease instead — but
            # the state machine's contract stays one shape.)
            raise _http(
                409,
                "OTHER_DEVICE_ONLINE",
                "Another device is currently online.",
                online_device_name_masked=result.online_device_name_masked,
                online_slot_no=result.online_slot_no,
                lease_expires_at=result.online_lease_expires_at,
            )

        aad = envelope_aad(operation, scope_candidates[0], key_digest)
        sealed_payload: dict[str, object] = {
            "user_id": device.user_id,
            "device_id": device.id,
            "session_id": result.session_id,
            "session_token": result.session_token or "",
            "session_epoch": result.session_epoch,
            "session_lease_expires_at": result.lease_until,
            "request_id": request_id,
            OUTCOME_FIELD: result.outcome,
        }
        ciphertext = seal_response(sealed_payload, key=aead_key, aad=aad)
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=ciphertext,
            key_version=aead_key_version,
            recovery_expires_at=_recovery_expires_at(conn),
        )

    outcome = result.outcome
    sealed_payload.pop(OUTCOME_FIELD, None)
    response.status_code = 200 if outcome == LOGIN_RENEWED else 201
    return LoginResponse.model_validate(sealed_payload)


@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=201,
    responses=RENEWAL_RESPONSES,
)
def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Drive the §12.3 login state machine (see the module docstring)."""
    return _establish_session_route(
        body=body,
        request=request,
        response=response,
        operation=LOGIN_OPERATION,
        takeover=False,
    )


# ---------------------------------------------------------------------------
# POST /switch
# ---------------------------------------------------------------------------


@router.post(
    "/switch",
    response_model=LoginResponse,
    status_code=201,
    responses=RENEWAL_RESPONSES,
)
def switch(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    """Drive the §12.3 explicit atomic switch (T20 / SES-02).

    The user confirmed the takeover on the client, so a live lease on the
    other device is displaced in one transaction instead of answering 409:
    the SWITCH event, the epoch bump and the fresh token commit together,
    and the old token is fenced out on every API instance the moment they
    do. Every other branch matches the login semantics (renewal, recovery,
    timeout takeover) — a switch never invents new states, and never kicks
    silently without the explicit client confirmation flow (SES-02 No-Go).
    """
    return _establish_session_route(
        body=body,
        request=request,
        response=response,
        operation=SWITCH_OPERATION,
        takeover=True,
    )


# ---------------------------------------------------------------------------
# POST /heartbeat
# ---------------------------------------------------------------------------


@router.post("/heartbeat", response_model=HeartbeatResponse)
def heartbeat(request: Request) -> HeartbeatResponse:
    """Renew the session lease (epoch untouched)."""
    _require_pg()
    _consume_customer_preauth(request)

    session_token = _bearer_token(request)
    if session_token is None:
        raise _http(401, "SESSION_TOKEN_REQUIRED", "A Bearer session token is required.")

    request_id = _request_id(request)
    with pg_transaction() as conn:
        try:
            # SES-01: the lease judgement runs on the in-transaction
            # PostgreSQL clock; the token digests need the device-domain key —
            # misconfiguration fails closed with 503 (never a 500, never a
            # client-credential error).
            verify_session_context(conn, presentation_session_token=session_token)
            result = heartbeat_session(
                conn,
                presentation_session_token=session_token,
                request_id=request_id,
                now=_transaction_now(conn),
            )
        except SessionFencingError as exc:
            raise _http(401, exc.code, exc.message) from exc
        except ActivationKeyError:
            logger.warning("device keys unavailable: configuration is incomplete")
            raise _http(
                503,
                "SESSION_SERVICE_UNAVAILABLE",
                "Device credential keys are not configured.",
            ) from None
        if result.outcome == HEARTBEAT_REPLACED:
            raise _http(401, "SESSION_REPLACED", "This session was replaced by another device.")
        if result.outcome == HEARTBEAT_EXPIRED:
            raise _http(401, "SESSION_EXPIRED", "This session lease has expired.")

    return HeartbeatResponse(
        session_id=result.session_id or "",
        session_epoch=result.session_epoch or 0,
        lease_expires_at=result.lease_until or "",
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response) -> None:
    """Pull the lease into the past and append the LOGOUT event."""
    _require_pg()
    _consume_customer_preauth(request)

    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")

    session_token = _bearer_token(request)
    if session_token is None:
        raise _http(401, "SESSION_TOKEN_REQUIRED", "A Bearer session token is required.")

    request_id = _request_id(request)
    req_hash = request_hash({"operation": LOGOUT_OPERATION})

    # Envelope keys + scope: fail closed with 503 when *either* key family
    # is misconfigured (the activation-route precedent). The scope is the
    # session token digest (probed across key versions).
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
        key_digests = idempotency_key_digests(idempotency_key)
        key_digest = key_digests[0]
        scope_candidates = list(reversed(_token_digests(session_token)))
    except (ActivationKeyError, IdempotencyKeyError):
        logger.warning("session keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "SESSION_SERVICE_UNAVAILABLE",
            "Session keys are not configured.",
        ) from None

    # Replay probe before authentication (the unbind precedent: the sealed 204
    # is the proof even though the token is by then expired).
    with pg_transaction() as conn:
        found = _find_envelope(
            conn,
            operation=LOGOUT_OPERATION,
            scopes=scope_candidates,
            key_digests=key_digests,
        )
    if found is not None:
        scope, matched_digest, existing, envelope_now = found
        _replay_logout_response(
            existing,
            req_hash=req_hash,
            scope=scope,
            key_digest=matched_digest,
            now=envelope_now,
        )
        response.headers[REPLAY_HEADER] = "true"
        return None

    with pg_transaction() as conn:
        envelope_id = insert_envelope(
            conn,
            operation=LOGOUT_OPERATION,
            scope=scope_candidates[0],
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            # A concurrent same-key writer committed first: replay its 204.
            # This transaction holds no writes of its own, so returning while
            # the block unwinds is safe.
            found = _find_envelope(
                conn,
                operation=LOGOUT_OPERATION,
                scopes=scope_candidates,
                key_digests=key_digests,
            )
            if found is None:
                raise _http(
                    409,
                    "IDEMPOTENCY_CONFLICT",
                    "The sealed response is no longer recoverable.",
                )
            scope, matched_digest, existing, envelope_now = found
            _replay_logout_response(
                existing,
                req_hash=req_hash,
                scope=scope,
                key_digest=matched_digest,
                now=envelope_now,
            )
            response.headers[REPLAY_HEADER] = "true"
            return None

        # SES-01: the PostgreSQL transaction clock drives the release.
        result = logout_session(
            conn,
            presentation_session_token=session_token,
            request_id=request_id,
            now=_transaction_now(conn),
        )
        if result.outcome == LOGOUT_REPLACED:
            raise _http(401, "SESSION_REPLACED", "This session was replaced by another device.")
        if result.outcome == LOGOUT_EXPIRED:
            raise _http(401, "SESSION_EXPIRED", "This session lease has expired.")

        aad = envelope_aad(LOGOUT_OPERATION, scope_candidates[0], key_digest)
        payload: dict[str, object] = {
            "session_id": result.session_id or "",
            "request_id": request_id,
        }
        ciphertext = seal_response(payload, key=aead_key, aad=aad)
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=ciphertext,
            key_version=aead_key_version,
            recovery_expires_at=_recovery_expires_at(conn),
        )

    return None
