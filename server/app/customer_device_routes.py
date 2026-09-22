"""T16 / DEV-01 + T17 / DEV-02 — device slot, pairing and unbind routes.

``GET /api/customer/devices`` and ``DELETE /api/customer/devices/{id}``
(dev doc §6.1), the application half of the frozen topic
``customer_device_routes.py`` (code checklist §3.2). Authentication is the
device credential — the long-lived secret returned once at bind time —
presented as ``Authorization: Bearer <device-token>``. The credential layer
and the online session are deliberately separate (dev doc §3.2): device
management keeps working across session leases, while T19/T21 layer the
session fencing on top.

``POST /api/customer/devices/enroll`` (T17 / DEV-02) is the six-step
second-device contract of dev doc §12.2, driven by the pairing state
machine:

- the second device submits the *main* activation code, its candidate
  fingerprint and an ``Idempotency-Key``; no wallet or order row is ever
  touched (DEV-02 No-Go — the second device never re-charges);
- with no active pairing request the route creates a ``PENDING`` row bound
  to the candidate digest and answers ``202`` — the request body never
  seals an envelope, because the answer carries no one-time secret and the
  pairing row itself is the retry-stable identity (same digest + same code
  → the same row, same answer);
- once the first bound device approves (``POST
  /api/customer/device-pairings/{id}/approve``), the *next* enroll with the
  same code + fingerprint consumes the approval: it takes the free slot,
  binds the ``BOUND`` row, flips the pairing to ``CONSUMED`` and answers
  ``201`` with the one-time device credential — sealed in the shared
  envelope engine so a client that lost the 201 replays the same
  ``device_id`` / slot / credential with the same key;
- device count is **unlimited**: migration 086 dropped the two-slot DB
  constraints and installed no replacement cap, and ``next_free_slot``
  always allocates the lowest free ordinal.  The 409 ``DEVICE_SLOTS_FULL``
  guards kept for that old cap are unreachable (see the notes at each raise
  and docs/decisions/DEVICE-CAPACITY-POLICY-20260922.md).

The approve route authenticates the *first* device's credential. Approving
is a monotone PENDING → APPROVED flip with no secret in the response, so
the state machine itself is the idempotency: a repeated approval answers
the current state (200), a consumed request answers 409
``PAIRING_ALREADY_CONSUMED`` and a lapsed one 409 ``PAIRING_EXPIRED`` (the
row flips to ``EXPIRED`` lazily in the same transaction). A pairing of
another activation code — or a random id — answers one 404
``PAIRING_NOT_FOUND`` (no IDOR oracle, the DELETE precedent).

Stable error codes (dev doc §13.2):

- 401 ``DEVICE_CREDENTIAL_REQUIRED`` — missing or malformed Authorization;
- 401 ``DEVICE_CREDENTIAL_INVALID`` — the token never resolved to a device
  of this deployment;
- 401 ``DEVICE_REVOKED`` — the token belonged to a device that has since
  been unbound or revoked; the client must wipe its stored credentials;
- 400 ``IDEMPOTENCY_KEY_REQUIRED`` — the DELETE/enroll carries no
  Idempotency-Key;
- 400 ``PAIRING_UNAVAILABLE`` — the unified anti-enumeration rejection for
  the enroll's code side (unknown, unactivated, suspended, expired batch
  — one answer, one latency profile, the T15 ACT-08 pattern);
- 404 ``DEVICE_NOT_FOUND`` — the DELETE target does not exist or belongs to
  another user (one answer, no IDOR oracle);
- 404 ``PAIRING_NOT_FOUND`` — the pairing does not exist or belongs to
  another activation code (one answer, no IDOR oracle);
- 409 ``DEVICE_ALREADY_UNBOUND`` — the target row is already released;
- 400 ``PAIRING_UNAVAILABLE`` — also covers a candidate fingerprint that
  already holds a current binding, so enrollment cannot be used as an
  account-existence oracle;
- 409 ``DEVICE_SLOTS_FULL`` — vestigial: written for the pre-086 two-slot
  cap and unreachable now that the count is unlimited; kept only so the
  retired pairing path can be removed whole under 任务 B;
- 409 ``PAIRING_EXPIRED`` / ``PAIRING_ALREADY_CONSUMED`` /
  ``PAIRING_SELF_APPROVAL`` — the one-shot pairing state machine;
- 409 ``IDEMPOTENCY_CONFLICT`` — the key was spent on a different request
  (request-hash mismatch) or is no longer recoverable (see below).

The DELETE carries an ``Idempotency-Key`` sealed with the shared envelope
engine (PR #47 Codex review P2). Its scope is derived from the target device
identifier and every replay remains behind credential authentication. That
prevents a globally reused low-entropy key from becoming a cross-account
ownership oracle; unauthenticated callers never receive another request's
request id or success replay.

The enroll envelope mirrors the T13 activation route exactly: operation
``device_enroll``, scope the candidate fingerprint digest (probed across
all configured key versions, highest first — a key scoped before a rotation
stays recoverable), request hash over the normalized body. The placeholder
insert runs only on the consumption branch, so a PENDING answer never
spends the key and a business failure (unified rejection, slots full)
rolls the placeholder back with the transaction — the key stays reusable.

The ``X-Request-Id`` header, when supplied, is echoed into the session audit
event; otherwise the route mints one (the activation-route precedent).

PostgreSQL is the customer source of truth: without a PG runtime both
routes fail closed with 503 (the SQLite lane keeps its internal P0 shape).
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field

from app.activation_code_service import (
    ActivationKeyError,
    InvalidActivationCodeError,
    activation_code_hmac_key,
    compute_code_digest,
    generate_activation_code,
    highest_code_hmac_key_version,
    iter_code_digests,
    mask_activation_code,
    normalize_activation_code,
)
from app.api_errors import http_error as _http
from app.auth_headers import bearer_token as _bearer_token
from app.customer_auth import CustomerSessionContext, SessionFencingError, verify_session_context
from app.customer_device_service import (
    APPROVE_ALREADY_CONSUMED,
    APPROVE_EXPIRED,
    APPROVE_FORBIDDEN,
    APPROVE_NOT_FOUND,
    APPROVE_REVOKED,
    APPROVE_SELF,
    OUTCOME_NOT_BOUND,
    OUTCOME_NOT_FOUND,
    PAIRING_APPROVED,
    PAIRING_PENDING,
    ActivePairing,
    AuthenticatedDevice,
    DeviceCredentialLookup,
    DeviceSlotsSnapshot,
    approve_pairing_request,
    consume_pairing_request,
    create_pairing_request,
    fingerprint_digests_for,
    highest_device_domain_key,
    list_device_slots,
    lookup_active_pairing,
    lookup_device_credential,
    next_free_slot,
    server_now_utc,
    unbind_device,
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
    seal_response,
)
from app.customer_idempotency import (
    request_hash as compute_request_hash,
)
from app.db_pg import get_pg_pool, pg_transaction
from app.ops_metrics import get_or_create_request_id
from app.security_rate_limit import (
    DIMENSION_ACTIVATE_CODE,
    DIMENSION_ACTIVATE_IP,
    DIMENSION_ACTIVATION_RESET_DEVICE,
    DIMENSION_ACTIVATION_RESET_IP,
    DIMENSION_CUSTOMER_PREAUTH_IP,
    RateLimitDecision,
    activation_code_limit,
    activation_ip_limit,
    activation_reset_device_limit,
    activation_reset_ip_limit,
    apply_anti_enumeration_delay,
    client_ip_from_request,
    consume_rate_limit,
    customer_preauth_ip_limit,
    failure_alert_active,
    failure_alert_threshold,
    rate_limit_window_seconds,
    record_auth_failure,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAY_HEADER = "X-Idempotent-Replay"
RETRY_AFTER_HEADER = "Retry-After"

UNBIND_OPERATION = "device_unbind"
RESET_CODE_OPERATION = "activation_code_reset"

# The enroll envelope operation; the scope is the candidate fingerprint
# digest (probed across configured key versions, the T13 activation
# precedent).
ENROLL_OPERATION = "device_enroll"

router = APIRouter(prefix="/api/customer", tags=["customer-devices"])


def _unbind_scope(user_id: str, device_id: str) -> str:
    """A non-reversible account/target namespace avoids cross-account key slots."""
    return hashlib.sha256(f"device-unbind:v2:{user_id}:{device_id}".encode()).hexdigest()


def _reset_code_scope(activation_code_id: str) -> str:
    return hashlib.sha256(f"activation-code-reset:v1:{activation_code_id}".encode()).hexdigest()


def _lock_reset_code_scope(conn: psycopg.Connection, scope: str) -> None:
    """Serialize reset keys for one activation code during its recovery window."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"{RESET_CODE_OPERATION}:{scope}",),
    )


class _PairingRaceLost(Exception):
    """A concurrent enroll of the same code + digest won the insert race."""


# ---------------------------------------------------------------------------
# Request / response contracts
# ---------------------------------------------------------------------------


class DeviceView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slot_no: int
    display_name: str
    platform: str
    status: str
    bound_at: str | None
    last_active_at: str | None
    unbound_at: str | None
    revoked_at: str | None
    is_current: bool


class DeviceSlotView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot_no: int
    device: DeviceView | None


class PendingPairingView(BaseModel):
    """A PENDING second-device pairing awaiting the first device's approval
    (T17 / DEV-02). The candidate's self-reported identity is shown to the
    approver; the keyed fingerprint digest never leaves the server."""

    model_config = ConfigDict(extra="forbid")

    pairing_request_id: str
    display_name: str
    platform: str
    created_at: str


class DeviceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slots: list[DeviceSlotView]
    history: list[DeviceView]
    pending_pairings: list[PendingPairingView] = []


class ActivationCodeResetResponse(BaseModel):
    """The replacement plaintext exists only in this no-store response."""

    model_config = ConfigDict(extra="forbid")

    activation_code: str
    masked_code: str


# ---------------------------------------------------------------------------
# Bearer authentication (device credential layer)
# ---------------------------------------------------------------------------


def _authenticate(conn: psycopg.Connection, token: str | None) -> AuthenticatedDevice:
    """Authenticate the request's device credential inside an open transaction.

    Raises the stable 401s; returns the authenticated device. The lookup
    runs on the caller's live connection so both routes re-validate the
    credential against the same transaction snapshot that serves the
    request — a credential released mid-flight cannot slip through.
    Key-configuration failures surface as 503 (the activation-route
    precedent): a server-side misconfiguration must never leak as a 500,
    and even less as a 401 that would trick the client into wiping its
    perfectly valid stored credentials (§13.2).
    """
    if token is None:
        raise _http(
            401,
            "DEVICE_CREDENTIAL_REQUIRED",
            "A Bearer device credential is required.",
        )
    try:
        lookup: DeviceCredentialLookup = lookup_device_credential(conn, token)
    except ActivationKeyError:
        logger.warning("device keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Device credential keys are not configured; device management is refused.",
        ) from None
    if lookup.device is not None:
        return lookup.device
    if lookup.row_status is not None:
        # The credential belonged to a device that has since been released:
        # the client-side contract is to wipe the stored credentials (§13.2).
        raise _http(
            401,
            "DEVICE_REVOKED",
            "This device credential has been revoked.",
        )
    raise _http(
        401,
        "DEVICE_CREDENTIAL_INVALID",
        "The device credential is invalid.",
    )


def _require_pg() -> None:
    """Fail closed with 503 when the PG runtime is not configured."""
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Device management requires the PostgreSQL runtime.",
        ) from exc


def _request_id(request: Request) -> str:
    return get_or_create_request_id(request)


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
        blocked = _http(429, "RATE_LIMITED", "Too many customer device requests.")
        blocked.headers = {RETRY_AFTER_HEADER: str(decision.retry_after_seconds)}
        raise blocked


def _insert_customer_device_audit(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    request_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_logs "
        "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            str(uuid4()),
            actor_user_id,
            action,
            entity_type,
            entity_id,
            json.dumps(
                {"request_id": request_id, **(metadata or {})},
                ensure_ascii=True,
                sort_keys=True,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _snapshot_response(
    snapshot: DeviceSlotsSnapshot, pending_pairings: list[PendingPairingView]
) -> DeviceListResponse:
    return DeviceListResponse(
        slots=[
            DeviceSlotView(
                slot_no=slot.slot_no,
                device=(
                    DeviceView.model_validate(slot.device) if slot.device is not None else None
                ),
            )
            for slot in snapshot.slots
        ],
        history=[DeviceView.model_validate(entry) for entry in snapshot.history],
        pending_pairings=pending_pairings,
    )


@router.get("/devices", response_model=DeviceListResponse)
def list_devices(request: Request) -> DeviceListResponse:
    """The two-slot status view: current bindings plus unbind history, and
    any PENDING second-device pairing awaiting this account's approval."""
    _require_pg()
    _consume_customer_preauth(request)
    token = _bearer_token(request)
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
        snapshot = list_device_slots(conn, user_id=device.user_id, current_device_id=device.id)
        pairing_rows = conn.execute(
            "SELECT id, display_name, platform, created_at "
            "FROM device_pairing_requests "
            "WHERE activation_code_id = %s AND status = 'PENDING' "
            "ORDER BY created_at, id",
            (device.activation_code_id,),
        ).fetchall()
    pending_pairings = [
        PendingPairingView(
            pairing_request_id=str(row[0]),
            display_name=str(row[1]),
            platform=str(row[2]),
            created_at=str(row[3]) if row[3] is not None else "",
        )
        for row in pairing_rows
    ]
    return _snapshot_response(snapshot, pending_pairings)


def _replay_unbind_response(
    record: EnvelopeRecord,
    *,
    req_hash: str,
    scope: str,
    key_digest: str,
) -> Response:
    """Replay the sealed 204 of a previously committed unbind.

    Same key + different target (request hash) answers 409; a purged,
    never-completed or lapsed envelope answers 409 — the key is spent. A
    ciphertext the configured keys cannot open is a server-side failure and
    answers 503 (the activation-route precedent).
    """
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
        or datetime.fromisoformat(str(record.recovery_expires_at)) <= datetime.now(UTC)
    ):
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key is no longer recoverable.",
        )
    try:
        replayed = open_response(
            str(record.ciphertext),
            key=customer_aead_key(int(record.key_version)),
            aad=envelope_aad(UNBIND_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "The device service cannot recover this response.",
        ) from None
    headers = {REPLAY_HEADER: "true"}
    replay_request_id = replayed.get("request_id")
    logger.info(
        "customer device unbind idempotent replay: key_version=%s request=%s",
        record.key_version,
        replay_request_id if isinstance(replay_request_id, str) else "-",
    )
    return Response(status_code=204, headers=headers)


@router.delete("/devices/{device_id}", status_code=204)
def unbind_device_route(device_id: str, request: Request) -> Response:
    """Unbind one of the caller's own devices; the row stays as history."""
    _require_pg()
    _consume_customer_preauth(request)
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "An Idempotency-Key header is required.",
        )
    token = _bearer_token(request)
    request_id = _request_id(request)
    req_hash = compute_request_hash({"device_id": device_id})
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
        key_digests = idempotency_key_digests(idempotency_key)
        key_digest = key_digests[0]
    except IdempotencyKeyError:
        logger.warning("idempotency AEAD keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Idempotency keys are not configured; device management is refused.",
        ) from None

    # Authenticate the presented credential before deriving or probing the
    # account-scoped envelope. A released credential may recover only the
    # response for that exact device; it never regains general device access.
    with pg_transaction() as conn:
        if token is None:
            raise _http(
                401,
                "DEVICE_CREDENTIAL_REQUIRED",
                "A Bearer device credential is required.",
            )
        try:
            credential = lookup_device_credential(conn, token)
        except ActivationKeyError:
            raise _http(
                503,
                "DEVICE_SERVICE_UNAVAILABLE",
                "Device credential keys are not configured; device management is refused.",
            ) from None
        matched_device = credential.matched_device
        if matched_device is None:
            raise _http(401, "DEVICE_CREDENTIAL_INVALID", "The device credential is invalid.")
        if credential.device is None and matched_device.id != device_id:
            raise _http(401, "DEVICE_REVOKED", "This device credential has been revoked.")
        scope = _unbind_scope(matched_device.user_id, device_id)
        matched = next(
            (
                (digest, record)
                for digest in key_digests
                if (
                    record := load_envelope(
                        conn,
                        operation=UNBIND_OPERATION,
                        scope=scope,
                        key_digest=digest,
                    )
                )
                is not None
            ),
            None,
        )
    if matched is not None:
        matched_digest, record = matched
        return _replay_unbind_response(
            record,
            req_hash=req_hash,
            scope=scope,
            key_digest=matched_digest,
        )
    if credential.device is None:
        raise _http(401, "DEVICE_REVOKED", "This device credential has been revoked.")

    recovery_seconds = recovery_window_seconds()
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
        envelope_id = insert_envelope(
            conn,
            operation=UNBIND_OPERATION,
            scope=scope,
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            # A concurrent same-key writer committed first: replay the
            # committed envelope instead of unbinding twice.
            matched = next(
                (
                    (digest, record)
                    for digest in key_digests
                    if (
                        record := load_envelope(
                            conn,
                            operation=UNBIND_OPERATION,
                            scope=scope,
                            key_digest=digest,
                        )
                    )
                    is not None
                ),
                None,
            )
            if matched is None:
                raise _http(
                    503,
                    "DEVICE_SERVICE_UNAVAILABLE",
                    "The unbind envelope could not be loaded after a key conflict.",
                )
            # This transaction holds no writes of its own (the insert above
            # lost the race), so answering from the sealed copy while the
            # block unwinds — committing nothing — is safe.
            matched_digest, loaded = matched
            return _replay_unbind_response(
                loaded,
                req_hash=req_hash,
                scope=scope,
                key_digest=matched_digest,
            )
        # SES-01: PostgreSQL is the only trusted clock — sample it inside
        # the transaction (the activation-route precedent) so unbound_at,
        # the pulled lease and the audit event share one server-side
        # timestamp instead of a possibly skewed application clock.
        now_row = conn.execute("SELECT now()").fetchone()
        now: datetime = now_row[0] if now_row is not None else server_now_utc()
        outcome = unbind_device(
            conn,
            device_id=device_id,
            owner_user_id=device.user_id,
            request_id=request_id,
            server_now=now,
        )
        if outcome == OUTCOME_NOT_FOUND:
            # Missing and foreign devices answer identically — the endpoint
            # must not become an enumeration oracle for other customers'
            # device ids.
            raise _http(
                404,
                "DEVICE_NOT_FOUND",
                "No such device for this account.",
            )
        if outcome == OUTCOME_NOT_BOUND:
            raise _http(
                409,
                "DEVICE_ALREADY_UNBOUND",
                "The device is already unbound.",
            )
        sealed_ciphertext = seal_response(
            {"device_id": device_id, "request_id": request_id},
            key=aead_key,
            aad=envelope_aad(UNBIND_OPERATION, scope, key_digest),
        )
        recovery_expires_at = (
            (now + timedelta(seconds=recovery_seconds)).replace(microsecond=0).isoformat()
        )
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=sealed_ciphertext,
            key_version=aead_key_version,
            recovery_expires_at=recovery_expires_at,
        )
    # Log only after the transaction committed: a rolled-back unbind must
    # not leave an audit log claiming success. Identifiers only, never the
    # token.
    logger.info(
        "customer device unbound: device=%s user=%s request=%s",
        device_id,
        device.user_id,
        request_id,
    )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# T17 / DEV-02 — second-device enroll and pairing approval
# ---------------------------------------------------------------------------


class DeviceEnrollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_code: str = Field(min_length=1, max_length=64)
    device_fingerprint: str = Field(min_length=1, max_length=512)
    device_name: str = Field(min_length=1, max_length=128)
    device_platform: str = Field(min_length=1, max_length=64)


class DeviceEnrollPendingResponse(BaseModel):
    """T28 (FE-01): the OpenAPI half of the 202 branch.

    Declared for the client's generated types only — the route still answers
    with a raw ``JSONResponse`` (the two bodies differ by design), so these
    models never gate the runtime; they exist so ``generated/api.ts`` can type
    the pairing wait without a hand-written drift.
    """

    model_config = ConfigDict(extra="forbid")

    pairing_request_id: str
    status: str
    expires_at: str
    request_id: str


class DeviceEnrollConsumedResponse(BaseModel):
    """T28 (FE-01): the OpenAPI half of the 201 branch (the one-time device
    credential handoff). Contract-only, like the pending model above."""

    model_config = ConfigDict(extra="forbid")

    device_id: str
    slot_no: int
    device_token: str
    request_id: str


# Contract-only response declarations for the enroll route: both bodies are
# JSONResponse at runtime (validation-free by design), the OpenAPI documents
# the two shapes so the T28 generated client types stay drift-free.
ENROLL_RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {
        "model": DeviceEnrollPendingResponse,
        "description": "The pairing request waits for the first device's "
        "approval (status PENDING, or APPROVED after a lost race — retry).",
    },
    201: {
        "model": DeviceEnrollConsumedResponse,
        "description": "An approved pairing was consumed: the one-time "
        "device credential for the second device.",
    },
}


class PairingApproveResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pairing_request_id: str
    status: str


def _unified_pairing_rejection() -> HTTPException:
    """The unified code-side rejection for the enroll lane (ACT-08 pattern).

    Unknown, unactivated, suspended, revoked and expired-batch codes share
    one answer body and one latency profile: the enroll accepts a plaintext
    code exactly like the activation route, so it must not become an
    enumeration oracle either.
    """
    return _http(400, "PAIRING_UNAVAILABLE", "The activation code cannot be used for pairing.")


def _is_unified_pairing_rejection(exc: HTTPException) -> bool:
    detail = exc.detail
    return (
        exc.status_code == 400
        and isinstance(detail, dict)
        and detail.get("code") == "PAIRING_UNAVAILABLE"
    )


def _audit_pairing_rejection(*, code_identifier: str, request_id: str) -> None:
    """Audit one unified enroll rejection and burn the constant delay.

    Same contract as the activation lane's ``_audit_code_rejection`` (T15 /
    ACT-08): the failure event lands in its own autocommit transaction so a
    rolled-back enroll never refunds the audit trail, recording is
    best-effort, and the constant delay runs even when the audit write
    fails — the timing profile must not depend on database health.
    """
    try:
        window = rate_limit_window_seconds()
        with pg_transaction() as conn:
            record_auth_failure(
                conn,
                dimension=DIMENSION_ACTIVATE_CODE,
                identifier=code_identifier,
                request_id=request_id,
            )
            if failure_alert_active(
                conn,
                dimension=DIMENSION_ACTIVATE_CODE,
                window_seconds=window,
                threshold=failure_alert_threshold(),
            ):
                logger.error(
                    "security alert: pairing code-side failures crossed the "
                    "alert threshold (window=%ss threshold=%s)",
                    window,
                    failure_alert_threshold(),
                )
    except Exception:
        logger.warning("security failure audit unavailable", exc_info=True)
    apply_anti_enumeration_delay()


def _replay_enroll_response(
    record: EnvelopeRecord,
    *,
    scope: str,
    req_hash: str,
    key_digest: str,
) -> Response:
    """Answer 201 from a committed enroll envelope (the strict judgement).

    Same key + different body answers 409; a purged, never-completed or
    lapsed envelope answers 409 — the key is spent. A ciphertext the
    configured keys cannot open is a server-side failure and answers 503
    (the activation-route precedent).
    """
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
        or datetime.fromisoformat(str(record.recovery_expires_at)) <= datetime.now(UTC)
    ):
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key is no longer recoverable.",
        )
    try:
        replayed = open_response(
            str(record.ciphertext),
            key=customer_aead_key(int(record.key_version)),
            aad=envelope_aad(ENROLL_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "The device service cannot recover this response.",
        ) from None
    headers = {REPLAY_HEADER: "true"}
    replay_request_id = replayed.get("request_id")
    if isinstance(replay_request_id, str) and replay_request_id:
        headers[REQUEST_ID_HEADER] = replay_request_id
    logger.info(
        "customer device enroll idempotent replay: scope=%s key_version=%s request=%s",
        scope,
        record.key_version,
        replay_request_id if isinstance(replay_request_id, str) else "-",
    )
    return JSONResponse(status_code=201, content=replayed, headers=headers)


def _probe_enroll_replay(
    conn: psycopg.Connection,
    *,
    scope_candidates: list[str],
    key_digests: list[str],
    req_hash: str,
) -> Response | None:
    """Read-only probe for a replayable enroll envelope (the loose twin).

    A fully validated, openable replay short-circuits before the shared rate
    limiter runs — a legitimate retry (the client lost the 201 carrying the
    one-time credential) spends no abuse budget (the T15 review P2 rule).
    Every other outcome returns ``None`` and the request flows on to the
    original limiter + transaction path, whose checks stay authoritative.
    """
    for scope_candidate in scope_candidates:
        for key_digest in key_digests:
            record = load_envelope(
                conn,
                operation=ENROLL_OPERATION,
                scope=scope_candidate,
                key_digest=key_digest,
            )
            if record is None:
                continue
            if record.request_hash != req_hash:
                return None
            if record.ciphertext is None or record.key_version is None:
                return None
            if record.recovery_expires_at is not None and (
                datetime.fromisoformat(str(record.recovery_expires_at)) <= datetime.now(UTC)
            ):
                return None
            try:
                open_response(
                    record.ciphertext,
                    key=customer_aead_key(record.key_version),
                    aad=envelope_aad(ENROLL_OPERATION, scope_candidate, key_digest),
                )
            except IdempotencyKeyError:
                return None
            return _replay_enroll_response(
                record,
                scope=scope_candidate,
                req_hash=req_hash,
                key_digest=key_digest,
            )
    return None


def _has_active_pairing_for_poll(
    *,
    code_digests: list[str],
    fingerprint_digests: list[str],
) -> bool:
    """Whether this enroll maps to an existing active pairing (a status poll).

    Reuses ``lookup_active_pairing`` so the free lane answers for exactly the
    rows the state machine below would answer for: every configured code-key
    and fingerprint-key version is probed, and a lapsed row is flipped to
    EXPIRED here and reported absent — the follow-up enroll (a genuine new
    attempt) stays on the budgeted path. An unknown or malformed code has no
    row to find, so enumeration attempts are never exempted.
    """
    if not code_digests:
        return False
    with pg_transaction() as conn:
        code_row = conn.execute(
            "SELECT id FROM activation_codes WHERE code_digest = ANY(%s)",
            (code_digests,),
        ).fetchone()
        if code_row is None:
            return False
        now_row = conn.execute("SELECT now()").fetchone()
        if now_row is None or not isinstance(now_row[0], datetime):
            # No trusted clock, no exemption — the budgeted path decides.
            return False
        return (
            lookup_active_pairing(
                conn,
                activation_code_id=str(code_row[0]),
                fingerprint_digests=fingerprint_digests,
                server_now=now_row[0],
            )
            is not None
        )


def _pending_response(pairing: ActivePairing, *, request_id: str) -> Response:
    # The status mirrors the row's actual state: the normal PENDING branch
    # answers "waiting for approval", while the race-lost re-read may catch
    # the winner's row already APPROVED — a 202 carrying status=APPROVED
    # tells the client to retry the enroll immediately and land on the
    # consumption branch (PR #49 Codex review P3).
    return JSONResponse(
        status_code=202,
        content={
            "pairing_request_id": pairing.id,
            "status": pairing.status,
            "expires_at": pairing.expires_at,
            "request_id": request_id,
        },
        headers={REQUEST_ID_HEADER: request_id},
    )


@router.post("/devices/enroll", status_code=202, responses=ENROLL_RESPONSES)
def enroll_second_device(body: DeviceEnrollRequest, request: Request) -> Response:
    """Start (or finish) the second-device pairing for one activation code.

    Two answers, driven by the pairing state machine: ``202`` while the
    request waits for the first device's approval, ``201`` with the one-time
    device credential when an approved pairing is consumed. The response
    body is a raw ``JSONResponse`` either way (the two shapes differ by
    design and must not be gated by response validation); the OpenAPI
    contract for both shapes is declared above (``ENROLL_RESPONSES``, the
    T28 client-type task) so the generated client types stay drift-free.
    The decorator's ``status_code=202`` mirrors the pending branch so
    FastAPI's default success status never materializes a phantom 200 in
    the contract (PR #55 review) — the route never answers 200.
    """
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(
            400,
            "IDEMPOTENCY_KEY_REQUIRED",
            "An Idempotency-Key header is required.",
        )

    # A normalization failure must not short-circuit: the request still
    # passes the shared limiter below so a malformed-code burst (format
    # probing) cannot skirt the abuse budget (the T15 activate pattern).
    canonical_code: str | None
    try:
        canonical_code = normalize_activation_code(body.activation_code)
    except InvalidActivationCodeError:
        canonical_code = None

    fingerprint = body.device_fingerprint.strip()
    device_name = body.device_name.strip()
    device_platform = body.device_platform.strip()
    if not fingerprint or not device_name or not device_platform:
        raise _http(
            400,
            "INVALID_DEVICE_INFO",
            "Device fingerprint, name and platform are required.",
        )

    _require_pg()
    _consume_customer_preauth(request)

    try:
        fingerprint_digests, fingerprint_key_version = fingerprint_digests_for(fingerprint)
        _, hmac_key = highest_device_domain_key()
        aead_key_version, aead_key = highest_customer_aead_key()
        code_digests = (
            [digest for digest, _version in iter_code_digests(canonical_code)]
            if canonical_code is not None
            else []
        )
    except (ActivationKeyError, IdempotencyKeyError):
        logger.warning("device pairing keys unavailable: configuration is incomplete")
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Device pairing keys are not configured; enrollment is refused.",
        ) from None

    fingerprint_hmac = fingerprint_digests[-1]
    scope_candidates = list(reversed(fingerprint_digests))
    key_digests = idempotency_key_digests(idempotency_key)
    key_digest = key_digests[0]
    req_hash = compute_request_hash(
        {
            "activation_code": canonical_code or "",
            "device_fingerprint": fingerprint,
            "device_name": device_name,
            "device_platform": device_platform,
        }
    )
    request_id = _request_id(request)

    # The read-only replay probe runs before the limiter: a retry of a lost
    # 201 (the one-time credential) replays without spending any budget.
    with pg_transaction() as conn:
        replayed = _probe_enroll_replay(
            conn,
            scope_candidates=scope_candidates,
            key_digests=key_digests,
            req_hash=req_hash,
        )
    if replayed is not None:
        return replayed

    # The 0.1.12 waiting screen auto-polls this endpoint every few seconds
    # for an existing pairing's outcome. A device that already holds an
    # unexpired PENDING/APPROVED pairing is polling, not probing codes, so
    # those requests skip the shared budgets below — otherwise the default
    # 5-per-window code budget 429s the poll within seconds and starves the
    # code's next legitimate activation attempt. Unknown codes, malformed
    # codes and expired pairings report no active row and stay budgeted, so
    # the ACT-08 enumeration surface itself is unchanged.
    pairing_status_poll = _has_active_pairing_for_poll(
        code_digests=code_digests,
        fingerprint_digests=fingerprint_digests,
    )

    # The shared limiter — the enroll accepts a plaintext code exactly like
    # the activation route, so it draws from the same IP and code budgets
    # (ACT-08: one enumeration attack surface, one budget per dimension).
    client_ip = client_ip_from_request(request)
    _window_seconds = rate_limit_window_seconds()
    ip_decision: RateLimitDecision | None = None
    code_decision: RateLimitDecision | None = None
    if not pairing_status_poll:
        with pg_transaction() as conn:
            ip_decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_ACTIVATE_IP,
                identifier=client_ip,
                limit=activation_ip_limit(),
                window_seconds=_window_seconds,
            )
            # A blocked IP draws no code budget (the PR #46 review P1 rule:
            # attacker-controlled identifiers must not mint unbounded rows).
            if code_digests and ip_decision.allowed:
                code_decision = consume_rate_limit(
                    conn,
                    dimension=DIMENSION_ACTIVATE_CODE,
                    identifier=code_digests[0],
                    limit=activation_code_limit(),
                    window_seconds=_window_seconds,
                )
    if ip_decision is not None and (
        not ip_decision.allowed or (code_decision is not None and not code_decision.allowed)
    ):
        retry_after = max(
            ip_decision.retry_after_seconds,
            code_decision.retry_after_seconds if code_decision is not None else 0,
        )
        blocked = _http(
            429,
            "RATE_LIMITED",
            "Too many enrollment attempts; retry later.",
        )
        blocked.headers = {RETRY_AFTER_HEADER: str(retry_after)}
        raise blocked

    if canonical_code is None:
        _audit_pairing_rejection(
            code_identifier="malformed",
            request_id=request_id,
        )
        raise _unified_pairing_rejection() from None

    recovery_seconds = recovery_window_seconds()
    unavailable = _unified_pairing_rejection()

    try:
        with pg_transaction() as conn:
            # Key reuse may have scoped its envelope under an older fingerprint
            # digest before a rotation: look through every configured version's
            # scope (highest first), the T13 precedent.
            found_scope: str | None = None
            record: EnvelopeRecord | None = None
            found_key_digest: str | None = None
            for scope_candidate in scope_candidates:
                for candidate_key_digest in key_digests:
                    loaded = load_envelope(
                        conn,
                        operation=ENROLL_OPERATION,
                        scope=scope_candidate,
                        key_digest=candidate_key_digest,
                    )
                    if loaded is not None:
                        found_scope = scope_candidate
                        found_key_digest = candidate_key_digest
                        record = loaded
                        break
                if record is not None:
                    break

            if record is not None:
                assert found_scope is not None
                assert found_key_digest is not None
                return _replay_enroll_response(
                    record,
                    scope=found_scope,
                    req_hash=req_hash,
                    key_digest=found_key_digest,
                )

            # SES-01: PostgreSQL is the only trusted clock — sample it inside
            # the transaction so the pairing expiry, the binding timestamps
            # and the envelope recovery window share one server clock.
            now_row = conn.execute("SELECT now()").fetchone()
            if now_row is None or not isinstance(now_row[0], datetime):
                raise _http(
                    503,
                    "DEVICE_SERVICE_UNAVAILABLE",
                    "The database clock is unavailable.",
                )
            server_now = now_row[0]

            # Step 4 of the §12.2 contract: lock the code row so concurrent
            # enrolls (and activations) serialize here.
            code_row = conn.execute(
                "SELECT c.id, c.status, c.bound_user_id, b.activation_expires_at "
                "FROM activation_codes c "
                "JOIN activation_code_batches b ON b.id = c.batch_id "
                "WHERE c.code_digest = ANY(%s) "
                "FOR UPDATE OF c",
                (code_digests,),
            ).fetchone()
            if code_row is None:
                raise unavailable
            code_id = str(code_row[0])
            code_status = str(code_row[1])
            bound_user_id = code_row[2]
            if code_status != "ACTIVE" or bound_user_id is None:
                # ISSUED (never activated), SUSPENDED, REVOKED, EXPIRED — one
                # answer, one latency profile (anti-enumeration).
                raise unavailable
            batch_expiry = datetime.fromisoformat(str(code_row[3]))
            if batch_expiry.tzinfo is None:
                batch_expiry = batch_expiry.replace(tzinfo=UTC)
            if batch_expiry <= server_now:
                raise unavailable

            # §12.2 step 4 (PR #49 Codex review P2): the current device rows
            # are locked *before* the pairing row so the occupancy snapshot
            # serializes against a concurrent unbind.  The 409 that lock was
            # originally written to avoid is unreachable now that there is no
            # device cap (docs/decisions/DEVICE-CAPACITY-POLICY-20260922.md),
            # but the snapshot-before-decide ordering stays correct, and the
            # shared lock order is code → devices → pairing (the approve route
            # walks its tail: devices → pairing), so the two routes cannot
            # deadlock.
            conn.execute(
                "SELECT slot_no FROM customer_devices "
                "WHERE activation_code_id = %s AND status = 'BOUND' FOR UPDATE",
                (code_id,),
            ).fetchall()

            # The candidate fingerprint must not already hold a binding
            # (checked across every configured key version plus the
            # revision-034 cross-version probe key, the PR #44 review P1 /
            # activation-route M2 rule — this is also what makes self-approval
            # of a pairing structurally impossible through the enroll path).
            bound = conn.execute(
                "SELECT 1 FROM customer_devices "
                "WHERE (fingerprint_hmac = ANY(%s) OR fingerprint_canonical = %s) "
                "AND status = 'BOUND'",
                (fingerprint_digests, fingerprint_digests[0]),
            ).fetchone()
            if bound is not None:
                raise _unified_pairing_rejection()

            pairing = lookup_active_pairing(
                conn,
                activation_code_id=code_id,
                fingerprint_digests=fingerprint_digests,
                server_now=server_now,
            )

            if pairing is not None and pairing.status == PAIRING_PENDING:
                # Waiting for the first device's approval: the pairing row is
                # the retry-stable identity, and the answer carries no
                # one-time secret — nothing is sealed, nothing is spent.
                return _pending_response(pairing, request_id=request_id)

            if pairing is not None and pairing.status == PAIRING_APPROVED:
                # Consumption: the only branch that seals an envelope, so a
                # lost 201 replays the one-time credential with this key.
                envelope_id = insert_envelope(
                    conn,
                    operation=ENROLL_OPERATION,
                    scope=fingerprint_hmac,
                    key_digest=key_digest,
                    request_hash=req_hash,
                )
                if envelope_id is None:
                    # A concurrent same-key writer committed the consumption
                    # first (the insert blocked on the unique index until the
                    # other transaction committed): answer from the winner's
                    # envelope. This transaction holds no writes of its own,
                    # so returning while the block unwinds is safe.
                    loaded = load_envelope(
                        conn,
                        operation=ENROLL_OPERATION,
                        scope=fingerprint_hmac,
                        key_digest=key_digest,
                    )
                    if loaded is None:
                        raise _http(
                            503,
                            "DEVICE_SERVICE_UNAVAILABLE",
                            "The enroll envelope could not be loaded after a key conflict.",
                        )
                    return _replay_enroll_response(
                        loaded,
                        scope=fingerprint_hmac,
                        req_hash=req_hash,
                        key_digest=key_digest,
                    )
                consumed = consume_pairing_request(
                    conn,
                    pairing=pairing,
                    hmac_key=hmac_key,
                    token_key_version=fingerprint_key_version,
                    owner_user_id=str(bound_user_id),
                    fingerprint_canonical=fingerprint_digests[0],
                    server_now=server_now,
                )
                if consumed is None:
                    # UNREACHABLE (task D, 2026-09-22): consume_pairing_request
                    # returns None only when next_free_slot does, and that
                    # always finds a free ordinal — the device count is
                    # unlimited.  Left in place so the retired pairing path
                    # can be removed whole under 任务 B; see
                    # docs/decisions/DEVICE-CAPACITY-POLICY-20260922.md.
                    raise _http(
                        409,
                        "DEVICE_SLOTS_FULL",
                        "Both device slots are currently bound.",
                    )
                payload = {
                    "device_id": consumed.device_id,
                    "slot_no": consumed.slot_no,
                    "device_token": consumed.device_token,
                    "request_id": request_id,
                }
                sealed_ciphertext = seal_response(
                    payload,
                    key=aead_key,
                    aad=envelope_aad(ENROLL_OPERATION, fingerprint_hmac, key_digest),
                )
                recovery_expires_at = (
                    (server_now + timedelta(seconds=recovery_seconds))
                    .replace(microsecond=0)
                    .isoformat()
                )
                complete_envelope(
                    conn,
                    envelope_id,
                    ciphertext=sealed_ciphertext,
                    key_version=aead_key_version,
                    recovery_expires_at=recovery_expires_at,
                )
                logger.info(
                    "customer device enrolled: device=%s slot=%s code=%s request=%s",
                    consumed.device_id,
                    consumed.slot_no,
                    code_id,
                    request_id,
                )
                return JSONResponse(
                    status_code=201,
                    content=payload,
                    headers={REQUEST_ID_HEADER: request_id},
                )

            # No active pairing: create the PENDING request.  The guard below
            # is UNREACHABLE (task D, 2026-09-22): next_free_slot allocates the
            # lowest free ordinal and never returns None, so a third device is
            # not a "third-device block" — the count is unlimited.  Kept only
            # so the retired pairing path can be removed whole under 任务 B;
            # see docs/decisions/DEVICE-CAPACITY-POLICY-20260922.md.
            if next_free_slot(conn, code_id) is None:
                raise _http(
                    409,
                    "DEVICE_SLOTS_FULL",
                    "Both device slots are currently bound.",
                )
            try:
                pairing = create_pairing_request(
                    conn,
                    activation_code_id=code_id,
                    candidate_fingerprint_hmac=fingerprint_hmac,
                    candidate_fingerprint_key_version=fingerprint_key_version,
                    display_name=device_name,
                    platform=device_platform,
                    server_now=server_now,
                )
            except UniqueViolation:
                # A concurrent enroll of the same code + digest won the
                # partial-unique race. Under the current route topology this
                # lane is defensively unreachable (same-code enrolls serialize
                # on the code-row lock), but it stays as depth against any
                # future writer that inserts pairing rows outside that lock
                # (PR #49 Codex review P3). This transaction is aborted, so
                # reload the winner's row in a fresh transaction.
                raise _PairingRaceLost from None
            logger.info(
                "customer pairing requested: code=%s candidate=%s request=%s",
                code_id,
                fingerprint_hmac,
                request_id,
            )
            return _pending_response(pairing, request_id=request_id)
    except _PairingRaceLost:
        with pg_transaction() as conn:
            now_row = conn.execute("SELECT now()").fetchone()
            server_now = now_row[0] if now_row is not None else server_now_utc()
            winner = lookup_active_pairing(
                conn,
                activation_code_id=code_id,
                fingerprint_digests=fingerprint_digests,
                server_now=server_now,
            )
        if winner is not None:
            # The 202 mirrors the winner's actual state (PENDING or APPROVED)
            # so the client retries the enroll and lands on the authoritative
            # branch — never a stale "still waiting" answer.
            return _pending_response(winner, request_id=request_id)
        # The winner's row already lapsed or was consumed between the two
        # transactions — vanishingly rare; the client retries with a fresh
        # key and lands on the authoritative state.
        raise _http(
            409,
            "PAIRING_CONFLICT",
            "The pairing request changed concurrently; retry.",
        ) from None
    except UniqueViolation as exc:
        constraint = exc.diag.constraint_name or ""
        if constraint in (
            "uq_customer_devices_fingerprint",
            # Revision 034 probe key: a cross-version digest collision is the
            # same "device already holds an activation" fact (the
            # activation-route M2 precedent).
            "uq_customer_devices_fingerprint_canonical",
        ):
            # Defensive: the candidate lost the partial-unique race against a
            # concurrent binding (the pre-check inside the code-row lock
            # already covers the normal path, the T13 precedent).
            raise _unified_pairing_rejection() from exc
        raise
    except HTTPException as exc:
        # The unified code-side rejection is audited and pays the constant
        # anti-enumeration delay on its way out (the T15 pattern).
        if _is_unified_pairing_rejection(exc):
            _audit_pairing_rejection(
                code_identifier=code_digests[0],
                request_id=request_id,
            )
        raise


@router.post(
    "/device-pairings/{pairing_id}/approve",
    response_model=PairingApproveResponse,
)
def approve_device_pairing(pairing_id: str, request: Request) -> PairingApproveResponse:
    """The first bound device approves a PENDING pairing request.

    The state machine is the idempotency: approving twice answers the
    current state, and no secret ever rides the response, so this route
    carries no envelope (the enroll's one-time credential is the sealed
    surface, §12.2 step 6).
    """
    _require_pg()
    _consume_customer_preauth(request)
    token = _bearer_token(request)
    request_id = _request_id(request)
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
        now_row = conn.execute("SELECT now()").fetchone()
        now: datetime = now_row[0] if now_row is not None else server_now_utc()
        outcome = approve_pairing_request(
            conn,
            pairing_id=pairing_id,
            approver_device=device,
            server_now=now,
        )
    if outcome == APPROVE_NOT_FOUND:
        # Missing and cross-code pairings answer identically — the endpoint
        # must not become an enumeration oracle for other customers'
        # pairing ids.
        raise _http(
            404,
            "PAIRING_NOT_FOUND",
            "No such pairing request for this device.",
        )
    if outcome == APPROVE_FORBIDDEN:
        # §12.2 step 3 (PR #49 GitHub Codex review P1): the approval is the
        # first currently-bound device's lane; once that device is
        # unavailable the lane moves to the administrator verification (the
        # T18 control API), never down to the surviving slot 2. The caller
        # is a legitimate device of this very code, so the refusal names
        # the lane instead of pretending the pairing does not exist.
        raise _http(
            403,
            "PAIRING_APPROVER_FORBIDDEN",
            "Only the first device may approve a pairing request.",
        )
    if outcome == APPROVE_REVOKED:
        # The approver's binding was released after the authentication
        # snapshot (a concurrent unbind won the race): the same 401 the
        # credential would get on any fresh request (PR #49 Codex review P1).
        raise _http(
            401,
            "DEVICE_REVOKED",
            "This device credential has been revoked.",
        )
    if outcome == APPROVE_EXPIRED:
        raise _http(
            409,
            "PAIRING_EXPIRED",
            "The pairing request has expired.",
        )
    if outcome == APPROVE_ALREADY_CONSUMED:
        raise _http(
            409,
            "PAIRING_ALREADY_CONSUMED",
            "The pairing request has already been consumed.",
        )
    if outcome == APPROVE_SELF:
        raise _http(
            409,
            "PAIRING_SELF_APPROVAL",
            "A device cannot approve its own pairing request.",
        )
    # APPROVE_APPROVED or APPROVE_ALREADY_APPROVED — both answer the state.
    logger.info(
        "customer pairing approved: pairing=%s approver_device=%s request=%s",
        pairing_id,
        device.id,
        request_id,
    )
    return PairingApproveResponse(
        pairing_request_id=pairing_id,
        status=PAIRING_APPROVED,
    )


@router.delete(
    "/device-pairings/{pairing_id}",
    status_code=204,
)
def dismiss_device_pairing(pairing_id: str, request: Request) -> Response:
    """Dismiss an invalid pending/approved pairing without deleting audit lineage."""
    _require_pg()
    _consume_customer_preauth(request)
    token = _bearer_token(request)
    request_id = _request_id(request)
    with pg_transaction() as conn:
        device = _authenticate(conn, token)
        device_row = conn.execute(
            "SELECT status FROM customer_devices WHERE id = %s FOR UPDATE",
            (device.id,),
        ).fetchone()
        if device_row is None or str(device_row[0]) != "BOUND":
            raise _http(401, "DEVICE_REVOKED", "This device credential has been revoked.")
        pairing = conn.execute(
            "SELECT status FROM device_pairing_requests "
            "WHERE id = %s AND activation_code_id = %s FOR UPDATE",
            (pairing_id, device.activation_code_id),
        ).fetchone()
        if pairing is None:
            raise _http(
                404,
                "PAIRING_NOT_FOUND",
                "No such pairing request for this device.",
            )
        pairing_status = str(pairing[0])
        if pairing_status == "EXPIRED":
            return Response(status_code=204)
        if pairing_status == "CONSUMED":
            raise _http(
                409,
                "PAIRING_ALREADY_CONSUMED",
                "The pairing request has already been consumed.",
            )
        conn.execute(
            "UPDATE device_pairing_requests SET status = 'EXPIRED' WHERE id = %s",
            (pairing_id,),
        )
        _insert_customer_device_audit(
            conn,
            actor_user_id=device.user_id,
            action="customer.device_pairing.dismissed",
            entity_type="device_pairing_request",
            entity_id=pairing_id,
            request_id=request_id,
            metadata={"previous_status": pairing_status},
        )
    logger.info(
        "customer pairing dismissed: pairing=%s actor_device=%s request=%s",
        pairing_id,
        device.id,
        request_id,
    )
    return Response(status_code=204)


def _replay_reset_code_response(
    record: EnvelopeRecord,
    *,
    scope: str,
    key_digest: str,
    req_hash: str,
    response: Response,
) -> ActivationCodeResetResponse:
    if record.request_hash != req_hash:
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key was already used for a different request.",
        )
    if (
        record.ciphertext is None
        or record.key_version is None
        or record.recovery_expires_at is None
        or record.purged_at is not None
        or datetime.fromisoformat(str(record.recovery_expires_at)) <= datetime.now(UTC)
    ):
        raise _http(
            409,
            "IDEMPOTENCY_CONFLICT",
            "This idempotency key is no longer recoverable.",
        )
    try:
        replayed = open_response(
            record.ciphertext,
            key=customer_aead_key(record.key_version),
            aad=envelope_aad(RESET_CODE_OPERATION, scope, key_digest),
        )
    except IdempotencyKeyError:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "The reset response cannot be recovered.",
        ) from None
    response.headers["Cache-Control"] = "no-store"
    response.headers[REPLAY_HEADER] = "true"
    return ActivationCodeResetResponse.model_validate(replayed)


@router.post(
    "/activation-code/reset",
    response_model=ActivationCodeResetResponse,
)
def reset_customer_activation_code(
    request: Request,
    response: Response,
) -> ActivationCodeResetResponse:
    """Rotate the main activation code from the live primary-device session.

    The old digest is replaced atomically. The replacement plaintext is not
    persisted and is returned exactly once with a no-store cache directive.
    Any unconsumed pairing created with the previous code is invalidated.
    """
    _require_pg()
    _consume_customer_preauth(request)
    idempotency_key = request.headers.get(IDEMPOTENCY_KEY_HEADER, "").strip()
    if not idempotency_key:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "An Idempotency-Key header is required.")
    token = _bearer_token(request)
    if token is None:
        raise _http(401, "SESSION_REQUIRED", "A live customer session is required.")
    request_id = _request_id(request)
    try:
        aead_key_version, aead_key = highest_customer_aead_key()
        key_digests = idempotency_key_digests(idempotency_key)
        key_digest = key_digests[0]
    except IdempotencyKeyError:
        raise _http(
            503,
            "DEVICE_SERVICE_UNAVAILABLE",
            "Idempotency keys are not configured.",
        ) from None
    req_hash = compute_request_hash({"operation": RESET_CODE_OPERATION})

    with pg_transaction() as conn:
        ip_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_ACTIVATION_RESET_IP,
            identifier=client_ip_from_request(request),
            limit=activation_reset_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    if not ip_decision.allowed:
        blocked = _http(429, "RATE_LIMITED", "Too many activation-code reset requests.")
        blocked.headers = {RETRY_AFTER_HEADER: str(ip_decision.retry_after_seconds)}
        raise blocked
    with pg_transaction() as conn:
        try:
            preliminary_session = verify_session_context(
                conn,
                presentation_session_token=token,
            )
        except ActivationKeyError:
            raise _http(
                503,
                "SESSION_SERVICE_UNAVAILABLE",
                "Session verification is unavailable.",
            ) from None
        except SessionFencingError as exc:
            raise _http(401, exc.code, exc.message) from None
        device_decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_ACTIVATION_RESET_DEVICE,
            identifier=preliminary_session.device_id,
            limit=activation_reset_device_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    if not device_decision.allowed:
        blocked = _http(429, "RATE_LIMITED", "Too many activation-code reset requests.")
        blocked.headers = {RETRY_AFTER_HEADER: str(device_decision.retry_after_seconds)}
        raise blocked

    with pg_transaction() as conn:
        try:
            session: CustomerSessionContext = verify_session_context(
                conn,
                presentation_session_token=token,
            )
        except ActivationKeyError:
            raise _http(
                503,
                "SESSION_SERVICE_UNAVAILABLE",
                "Session verification is unavailable.",
            ) from None
        except SessionFencingError as exc:
            raise _http(401, exc.code, exc.message) from None
        if session.activation_code_id is None:
            raise _http(400, "ACTIVATION_CODE_NOT_APPLICABLE", "账号密码用户无需激活码。")
        scope = _reset_code_scope(session.activation_code_id)
        _lock_reset_code_scope(conn, scope)
        matched = next(
            (
                (digest, record)
                for digest in key_digests
                if (
                    record := load_envelope(
                        conn,
                        operation=RESET_CODE_OPERATION,
                        scope=scope,
                        key_digest=digest,
                    )
                )
                is not None
            ),
            None,
        )
        if matched is not None:
            matched_digest, record = matched
            return _replay_reset_code_response(
                record,
                scope=scope,
                key_digest=matched_digest,
                req_hash=req_hash,
                response=response,
            )
        active_other_key = conn.execute(
            "SELECT 1 FROM customer_idempotency_envelopes "
            "WHERE operation = %s AND scope = %s "
            "AND NOT (key_digest = ANY(%s)) "
            "AND purged_at IS NULL AND ciphertext IS NOT NULL "
            "AND recovery_expires_at::timestamptz > now() LIMIT 1",
            (RESET_CODE_OPERATION, scope, key_digests),
        ).fetchone()
        if active_other_key is not None:
            raise _http(
                409,
                "IDEMPOTENCY_CONFLICT",
                "A previous activation-code reset is still recoverable with its original key.",
            )
        activation_row = conn.execute(
            "SELECT first_device_id FROM activation_code_activations "
            "WHERE code_id = %s AND user_id = %s",
            (session.activation_code_id, session.user_id),
        ).fetchone()
        if activation_row is None or str(activation_row[0]) != session.device_id:
            raise _http(
                403,
                "PRIMARY_DEVICE_REQUIRED",
                "Only the primary bound device may reset the activation code.",
            )
        code_row = conn.execute(
            "SELECT status FROM activation_codes WHERE id = %s FOR UPDATE",
            (session.activation_code_id,),
        ).fetchone()
        if code_row is None or str(code_row[0]) != "ACTIVE":
            raise _http(
                409,
                "ACTIVATION_CODE_NOT_ACTIVE",
                "Only an active authorization can be reset.",
            )
        envelope_id = insert_envelope(
            conn,
            operation=RESET_CODE_OPERATION,
            scope=scope,
            key_digest=key_digest,
            request_hash=req_hash,
        )
        if envelope_id is None:
            winner = load_envelope(
                conn,
                operation=RESET_CODE_OPERATION,
                scope=scope,
                key_digest=key_digest,
            )
            if winner is None:
                raise _http(
                    503,
                    "DEVICE_SERVICE_UNAVAILABLE",
                    "The reset envelope could not be loaded after a key conflict.",
                )
            return _replay_reset_code_response(
                winner,
                scope=scope,
                key_digest=key_digest,
                req_hash=req_hash,
                response=response,
            )
        try:
            key_version = highest_code_hmac_key_version()
            hmac_key = activation_code_hmac_key(key_version)
        except ActivationKeyError:
            raise _http(
                503,
                "ACTIVATION_SERVICE_UNAVAILABLE",
                "Activation-code keys are not configured.",
            ) from None

        replacement = ""
        replacement_digest = ""
        for _ in range(3):
            candidate = generate_activation_code()
            candidate_digest = compute_code_digest(candidate, key=hmac_key)
            collision = conn.execute(
                "SELECT 1 FROM activation_codes WHERE code_digest = %s",
                (candidate_digest,),
            ).fetchone()
            if collision is None:
                replacement = candidate
                replacement_digest = candidate_digest
                break
        if not replacement:
            raise _http(
                503,
                "ACTIVATION_CODE_GENERATION_FAILED",
                "A replacement activation code could not be generated.",
            )
        masked = mask_activation_code(replacement)
        conn.execute(
            "UPDATE activation_codes "
            "SET code_digest = %s, digest_key_version = %s, masked_code = %s "
            "WHERE id = %s",
            (
                replacement_digest,
                key_version,
                masked,
                session.activation_code_id,
            ),
        )
        expired_pairings = conn.execute(
            "UPDATE device_pairing_requests SET status = 'EXPIRED' "
            "WHERE activation_code_id = %s AND status IN ('PENDING', 'APPROVED')",
            (session.activation_code_id,),
        ).rowcount
        _insert_customer_device_audit(
            conn,
            actor_user_id=session.user_id,
            action="customer.activation_code.rotated",
            entity_type="activation_code",
            entity_id=session.activation_code_id,
            request_id=request_id,
            metadata={"expired_pairing_count": expired_pairings},
        )
        sealed_payload: dict[str, object] = {
            "activation_code": replacement,
            "masked_code": masked,
        }
        sealed = seal_response(
            sealed_payload,
            key=aead_key,
            aad=envelope_aad(RESET_CODE_OPERATION, scope, key_digest),
        )
        now_row = conn.execute("SELECT now()").fetchone()
        server_now = now_row[0] if now_row is not None else server_now_utc()
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=sealed,
            key_version=aead_key_version,
            recovery_expires_at=(server_now + timedelta(seconds=recovery_window_seconds()))
            .replace(microsecond=0)
            .isoformat(),
        )
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "customer activation code rotated: code=%s actor_device=%s request=%s",
        session.activation_code_id,
        session.device_id,
        request_id,
    )
    return ActivationCodeResetResponse(
        activation_code=replacement,
        masked_code=masked,
    )
