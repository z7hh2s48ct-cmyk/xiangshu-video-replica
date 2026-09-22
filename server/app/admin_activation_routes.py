"""T12 / ACT-04 — admin activation-code management API.

Application layer on top of the T10 catalog (revision 027) and the T11
service primitives: batch creation, generation + one-time AEAD export
creation, the audited plaintext download, delivery, suspension, resume,
revocation and listing — every write behind the T09 admin session / CSRF /
RBAC gate plus the admin write contract (dev doc §15: real actor, reason,
confirmation, Idempotency-Key, request id).

Write idempotency (dev doc §6.3 / §11.3, table from revision 031): each
business write runs inside one PostgreSQL transaction that first inserts an
``admin_write_idempotency`` placeholder keyed by (actor, canonical route,
key digest) with ``INSERT ... ON CONFLICT DO NOTHING``. The winner back-fills
the response snapshot before commit; a same-key retry replays the stored
response (``X-Idempotent-Replay: true``), and the same key against a
different canonical request is rejected with ``IDEMPOTENCY_CONFLICT``. A
business failure rolls the placeholder back with the transaction, so the
key stays reusable. Concurrent same-key writers serialize on the unique
index insert (PostgreSQL waits on the conflicting transaction).

The one-time download deliberately stays *outside* the snapshot layer: its
response carries the plaintext codes, which must never persist anywhere
(No-Go red line), and the ``downloaded_at`` one-shot constraint already
makes a second download impossible. A later copy is an explicit, audited
single-code reveal: list responses stay masked and never bulk-decrypt every
code visible on the page.

No-Go red lines: plaintext activation codes are never stored in a column,
event, idempotency snapshot or log record. Generation responses carry masked
codes only; the export ciphertext is opened only in memory for the audited
download or an authenticated administrator's detail view.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response

from app.activation_code_service import (
    ActivationCodeError,
    ActivationExportError,
    ActivationKeyError,
    ExportAlreadyDownloadedError,
    ExportExpiredError,
    ExportKeyUnavailableError,
    ExportPackageNotFoundError,
    InvalidActivationCodeError,
    InvalidCodeTransitionError,
    activation_code_hmac_key,
    assert_code_transition,
    configured_export_aead_keys,
    create_batch_export,
    decrypt_code_package,
    export_aead_key,
    fetch_export_package,
    generate_batch_codes,
    highest_code_hmac_key_version,
    highest_export_aead_key_version,
    iter_code_digests,
)
from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import (
    REPLAY_HEADER,
    REQUEST_ID_HEADER,
    AdminWriteContract,
)
from app.admin_write_contract import (
    begin_idempotent_write as _begin_idempotent_write,
)
from app.admin_write_contract import (
    canonical_route as _canonical_route,
)
from app.admin_write_contract import (
    finish_idempotent_write as _finish_idempotent_write,
)
from app.admin_write_contract import (
    http_error as _http,
)
from app.admin_write_contract import (
    load_idempotent_snapshot as _load_idempotent_snapshot,
)
from app.admin_write_contract import (
    request_hash as _request_hash,
)
from app.admin_write_contract import (
    require_write_contract as _require_write_contract,
)
from app.admin_write_contract import (
    transaction_now_iso as _transaction_now_iso,
)
from app.admin_write_contract import (
    write_with_idempotency as _write_with_idempotency,
)
from app.customer_session_service import (
    REASON_CODE_REVOKED,
    REASON_CODE_SUSPENDED,
    revoke_session,
)
from app.db_pg import pg_transaction
from app.ops_metrics import get_or_create_request_id

logger = logging.getLogger(__name__)

DEFAULT_EXPORT_TTL_SECONDS = 15 * 60
DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200

# M2 review H2 / PRICE-01: customer-sale batches stay unmintable until the
# price-freeze decision lands. Before this the only guard was a doc note —
# any admin writer could mint a "1-fen face value / 1M credits" batch. The
# mechanism is an explicit env opt-in; everything else fails closed.
BATCH_CREATION_ENV = "VIDEO_REPLICA_ALLOW_ACTIVATION_BATCH_CREATION"

router = APIRouter(prefix="/api/control", tags=["admin-activation"])


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Batch creation
# ---------------------------------------------------------------------------


class BatchCreateRequest(AdminWriteContract):
    name: str
    face_value_fen: int
    credits: int
    quantity: int
    activation_expires_at: str
    confirm_grant: bool = False


def _validate_batch_payload(body: BatchCreateRequest) -> None:
    problems: list[str] = []
    if not body.name.strip():
        problems.append("name must not be blank")
    if body.face_value_fen < 0:
        problems.append("face_value_fen must not be negative")
    if body.credits < 0 or body.credits > 2147483647:
        problems.append("credits must be within the nonnegative ledger integer range")
    if body.face_value_fen > 0 and body.credits == 0:
        problems.append("paid batches require positive credits")
    if body.face_value_fen == 0 and body.credits > 0 and not body.confirm_grant:
        problems.append("initial free seconds require confirm_grant")
    if body.quantity <= 0:
        problems.append("quantity must be positive")
    try:
        expires_at = datetime.fromisoformat(body.activation_expires_at)
    except ValueError:
        problems.append("activation_expires_at must be an ISO-8601 timestamp")
    else:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= datetime.now(UTC):
            problems.append("activation_expires_at must be in the future")
    if problems:
        raise _http(400, "BATCH_VALIDATION_FAILED", "; ".join(problems))


def _assert_batch_creation_allowed() -> None:
    """Fail closed unless the operator explicitly opted in (PRICE-01).

    Called before the idempotency envelope so probes during the freeze
    never consume an Idempotency-Key — the same key mints cleanly once the
    opt-in is set.
    """
    if os.environ.get(BATCH_CREATION_ENV, "").strip().lower() != "true":
        raise _http(
            403,
            "BATCH_CREATION_DISABLED",
            "Activation-code batch creation is disabled until the PRICE-01 "
            "customer-price decision freezes; set "
            f"{BATCH_CREATION_ENV}=true to opt in explicitly.",
        )


@router.post("/activation-code-batches", status_code=201)
def create_activation_code_batch(
    body: BatchCreateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Create an OPEN batch with frozen commercial snapshots."""
    _assert_batch_creation_allowed()

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        _validate_batch_payload(body)
        batch_id = str(uuid.uuid4())
        name = body.name.strip()
        unit_price = body.face_value_fen
        if body.face_value_fen == 0 and body.credits > 0:
            snapshot = conn.execute(
                "SELECT internal_base_unit_price_fen FROM runtime_settings WHERE id=1"
            ).fetchone()
            if snapshot is None:
                raise _http(503, "BILLING_SNAPSHOT_UNAVAILABLE", "Billing snapshot not configured.")
            unit_price = int(snapshot[0])
        if body.credits * unit_price > 2147483647:
            raise _http(
                400,
                "BATCH_VALIDATION_FAILED",
                "The credit calculation exceeds the ledger integer range.",
            )
        conn.execute(
            "INSERT INTO activation_code_batches "
            "(id, name, face_value_fen, unit_price_fen_snapshot, credits_snapshot, "
            " quantity, activation_expires_at, status, created_by_user_id, "
            " creation_reason, creation_request_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)",
            (
                batch_id,
                name,
                body.face_value_fen,
                # 免费赠送保留基础价作审计快照，收款金额仍由面值 0 决定。
                unit_price,
                body.credits,
                body.quantity,
                body.activation_expires_at,
                actor.user_id,
                # M2 review H1: the validated reason must not evaporate —
                # the batch row is the durable "why was this minted" record
                # (031 download-audit precedent).
                body.reason.strip(),
                request_id,
            ),
        )
        # 批次铸造是"凭空造码"的起点，与 archive/reveal 同属不能只留在业务表的
        # 动作（031 download-audit 先例）。批次行本身只回答"造了什么"，这里回答
        # "谁在什么时候按什么理由造的"，供审计中心统一 UNION 查询。
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'admin.activation_code_batch.created', "
            "'activation_code_batch', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                batch_id,
                json.dumps(
                    {
                        "request_id": request_id,
                        "reason": body.reason.strip(),
                        "quantity": body.quantity,
                        "face_value_fen": body.face_value_fen,
                        "credits": body.credits,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )
        logger.info(
            "activation code batch created: batch=%s quantity=%d actor=%s request=%s",
            batch_id,
            body.quantity,
            actor.user_id,
            request_id,
        )
        return {
            "batch_id": batch_id,
            "name": name,
            "face_value_fen": body.face_value_fen,
            "unit_price_fen_snapshot": unit_price,
            "credits_snapshot": body.credits,
            "quantity": body.quantity,
            "activation_expires_at": body.activation_expires_at,
            "status": "OPEN",
            "created_by_user_id": actor.user_id,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# Generation + one-time AEAD export creation
# ---------------------------------------------------------------------------


class GenerateRequest(AdminWriteContract):
    quantity: int
    auto_issue: bool = False


def _resolve_generation_keys() -> tuple[int, bytes, int, bytes]:
    """Resolve the highest configured HMAC / AEAD keys for new material."""
    hmac_version = highest_code_hmac_key_version()
    hmac_key = activation_code_hmac_key(hmac_version)
    aead_version = highest_export_aead_key_version()
    aead_key = export_aead_key(aead_version)
    return hmac_version, hmac_key, aead_version, aead_key


@router.post("/activation-code-batches/{batch_id}/generate", status_code=201)
def generate_activation_codes(
    batch_id: str,
    body: GenerateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Mint codes for an OPEN batch and seal them into one export package.

    Generation and export sealing share one transaction: the plaintext exists
    only inside that transaction's memory and lands in exactly two places —
    the AEAD envelope column (sealed) and the one-time download response. The
    API response itself carries masked codes only.
    """
    try:
        hmac_version, hmac_key, aead_version, aead_key = _resolve_generation_keys()
    except ActivationKeyError as exc:
        logger.warning("activation keys unavailable for generation: %s", type(exc).__name__)
        raise _http(
            503,
            "ACTIVATION_KEYS_UNAVAILABLE",
            "Activation code keys are not configured; generation is refused.",
        ) from exc

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if body.quantity < 1:
            raise _http(400, "BATCH_VALIDATION_FAILED", "quantity must be at least 1")
        batch_row = conn.execute(
            "SELECT status FROM activation_code_batches WHERE id = %s FOR UPDATE",
            (batch_id,),
        ).fetchone()
        if batch_row is None:
            raise _http(404, "BATCH_NOT_FOUND", "Unknown activation code batch.")
        if str(batch_row[0]) != "OPEN":
            raise _http(409, "BATCH_NOT_OPEN", "The batch is closed; codes cannot be minted.")
        try:
            generated = generate_batch_codes(
                conn,
                batch_id,
                quantity=body.quantity,
                key_version=hmac_version,
                hmac_key=hmac_key,
                actor_user_id=actor.user_id,
                request_id=request_id,
                reason=body.reason.strip(),
            )
        except ActivationCodeError as exc:
            raise _http(
                409,
                "BATCH_BUDGET_EXCEEDED",
                "Generating this quantity would exceed the frozen batch budget.",
            ) from exc
        export_id = create_batch_export(
            conn,
            batch_id,
            generated,
            requested_by_user_id=actor.user_id,
            ttl_seconds=DEFAULT_EXPORT_TTL_SECONDS,
            key_version=aead_version,
            aead_key=aead_key,
            request_id=request_id,
        )
        if body.auto_issue:
            issued_at = _transaction_now_iso(conn)
            reason = body.reason.strip()
            for code in generated:
                conn.execute(
                    "INSERT INTO activation_code_deliveries "
                    "(id, code_id, channel, external_order_ref, recipient_ref, "
                    " delivered_by_user_id, note) "
                    "VALUES (%s, %s, 'admin_console', NULL, NULL, %s, %s)",
                    (str(uuid.uuid4()), code.code_id, actor.user_id, reason),
                )
                conn.execute(
                    "UPDATE activation_codes SET status = 'ISSUED', issued_at = %s WHERE id = %s",
                    (issued_at, code.code_id),
                )
                conn.execute(
                    "INSERT INTO activation_code_events "
                    "(id, code_id, event, actor_user_id, reason, request_id) "
                    "VALUES (%s, %s, 'DELIVERED', %s, %s, %s)",
                    (
                        str(uuid.uuid4()),
                        code.code_id,
                        actor.user_id,
                        reason,
                        request_id,
                    ),
                )
        expires_row = conn.execute(
            "SELECT expires_at FROM activation_code_exports WHERE id = %s",
            (export_id,),
        ).fetchone()
        logger.info(
            "activation codes generated: batch=%s count=%d export=%s actor=%s request=%s",
            batch_id,
            len(generated),
            export_id,
            actor.user_id,
            request_id,
        )
        return {
            "batch_id": batch_id,
            "export_id": export_id,
            "expires_at": str(expires_row[0]) if expires_row is not None else "",
            "codes": [
                {"code_id": code.code_id, "masked_code": code.masked_code} for code in generated
            ],
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# One-time audited export download
# ---------------------------------------------------------------------------


class DownloadRequest(AdminWriteContract):
    pass


@router.post("/activation-code-exports/{export_id}/download")
def download_activation_code_export(
    export_id: str,
    body: DownloadRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """The single plaintext delivery path for an export package.

    Deliberately outside the idempotency snapshot layer: the response carries
    plaintext codes that must never persist (No-Go), and the one-time
    ``downloaded_at`` constraint already refuses any second download. The
    write contract (key / confirm / reason) is still enforced, and the reason
    + request id land in the durable export audit columns (PR #43 review P1).
    """
    _key, reason = _require_write_contract(request, body)
    request_id = get_or_create_request_id(request)
    try:
        aead_keys = configured_export_aead_keys()
        with pg_transaction() as conn:
            try:
                codes = fetch_export_package(
                    conn,
                    export_id,
                    downloaded_by_user_id=actor.user_id,
                    aead_keys=aead_keys,
                    download_reason=reason,
                    download_request_id=request_id,
                )
            except InvalidActivationCodeError as exc:
                # A decryptable package whose inner payload is malformed is
                # data rot of that one export, not a service outage; without
                # this branch the error escapes as an uncontrolled 500
                # (M2 review LOW).
                raise _http(
                    400,
                    "EXPORT_PACKAGE_INVALID",
                    "The export package could not be decoded; generate a new one.",
                ) from exc
            except ExportPackageNotFoundError as exc:
                raise _http(404, "EXPORT_NOT_FOUND", "Unknown export package.") from exc
            except ExportAlreadyDownloadedError as exc:
                raise _http(
                    409,
                    "EXPORT_ALREADY_DOWNLOADED",
                    "This export package was already downloaded exactly once.",
                ) from exc
            except ExportExpiredError as exc:
                raise _http(
                    409,
                    "EXPORT_EXPIRED",
                    "This export package has expired; generate a new one.",
                ) from exc
            except ExportKeyUnavailableError as exc:
                raise _http(
                    503,
                    "ACTIVATION_KEYS_UNAVAILABLE",
                    "The export key version is not configured; download is refused.",
                ) from exc
            except ActivationExportError as exc:
                # Typed subclasses above carry the operator-facing statuses; a
                # bare ActivationExportError here is an unexpected package
                # failure and refuses closed rather than guessing from text.
                raise _http(
                    503,
                    "ACTIVATION_KEYS_UNAVAILABLE",
                    "The export package could not be read; download is refused.",
                ) from exc
            audit_row = conn.execute(
                "SELECT batch_id, downloaded_at FROM activation_code_exports WHERE id = %s",
                (export_id,),
            ).fetchone()
    except RuntimeError as exc:
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Activation code management requires the PostgreSQL runtime.",
        ) from exc
    payload: dict[str, object] = {
        "export_id": export_id,
        "batch_id": str(audit_row[0]) if audit_row is not None else "",
        "codes": codes,
        "downloaded_at": str(audit_row[1]) if audit_row is not None else "",
        "request_id": request_id,
    }
    response.headers[REQUEST_ID_HEADER] = request_id
    # Plaintext codes never reach the logs — only counts and identifiers.
    logger.info(
        "activation export downloaded: export=%s actor=%s codes=%d request=%s reason=%s",
        export_id,
        actor.user_id,
        len(codes),
        request_id,
        reason,
    )
    return payload


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


class DeliverRequest(AdminWriteContract):
    channel: str
    external_order_ref: str | None = None
    recipient_ref: str | None = None


@router.post("/activation-codes/{code_id}/deliver", status_code=201)
def deliver_activation_code(
    code_id: str,
    body: DeliverRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Record a channel delivery and flip the code from GENERATED to ISSUED."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        channel = body.channel.strip()
        if not channel:
            raise _http(400, "DELIVERY_VALIDATION_FAILED", "channel must not be blank")
        code_row = conn.execute(
            "SELECT status FROM activation_codes WHERE id = %s FOR UPDATE",
            (code_id,),
        ).fetchone()
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        try:
            assert_code_transition(str(code_row[0]), "ISSUED")
        except InvalidCodeTransitionError as exc:
            raise _http(
                409,
                "CODE_TRANSITION_INVALID",
                "Only a GENERATED code can be delivered.",
            ) from exc
        now = _now_iso()
        delivery_id = str(uuid.uuid4())
        reason = body.reason.strip()
        conn.execute(
            "INSERT INTO activation_code_deliveries "
            "(id, code_id, channel, external_order_ref, recipient_ref, "
            " delivered_by_user_id, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                delivery_id,
                code_id,
                channel,
                body.external_order_ref,
                body.recipient_ref,
                actor.user_id,
                reason,
            ),
        )
        conn.execute(
            "UPDATE activation_codes SET status = 'ISSUED', issued_at = %s WHERE id = %s",
            (now, code_id),
        )
        conn.execute(
            "INSERT INTO activation_code_events "
            "(id, code_id, event, actor_user_id, reason, request_id) "
            "VALUES (%s, %s, 'DELIVERED', %s, %s, %s)",
            (str(uuid.uuid4()), code_id, actor.user_id, reason, request_id),
        )
        logger.info(
            "activation code delivered: code=%s channel=%s actor=%s request=%s",
            code_id,
            channel,
            actor.user_id,
            request_id,
        )
        return {
            "code_id": code_id,
            "status": "ISSUED",
            "delivery_id": delivery_id,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# Suspension, resume and revocation
# ---------------------------------------------------------------------------


def _locked_code_status(
    conn: psycopg.Connection, code_id: str, columns: str
) -> tuple[object, ...] | None:
    return conn.execute(
        f"SELECT {columns} FROM activation_codes WHERE id = %s FOR UPDATE",  # noqa: S608
        (code_id,),
    ).fetchone()


def _record_code_event(
    conn: psycopg.Connection,
    *,
    code_id: str,
    event: str,
    actor_user_id: str,
    reason: str,
    request_id: str,
) -> None:
    conn.execute(
        "INSERT INTO activation_code_events "
        "(id, code_id, event, actor_user_id, reason, request_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (str(uuid.uuid4()), code_id, event, actor_user_id, reason, request_id),
    )


def _transition_error(current: str, target: str) -> HTTPException:
    return _http(
        409,
        "CODE_TRANSITION_INVALID",
        f"The activation code cannot move from {current} to {target}.",
    )


@router.post("/activation-codes/{code_id}/suspend")
def suspend_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Suspend a delivered code (operator side state, frozen matrix)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status, bound_user_id")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        current = str(code_row[0])
        try:
            assert_code_transition(current, "SUSPENDED")
        except InvalidCodeTransitionError as exc:
            raise _transition_error(current, "SUSPENDED") from exc
        bound_user_id = code_row[1]
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET status = 'SUSPENDED', suspended_at = %s WHERE id = %s",
            (_now_iso(), code_id),
        )
        _record_code_event(
            conn,
            code_id=code_id,
            event="SUSPENDED",
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
        )
        # T20 / SES-03 revocation propagation: the suspension terminates the
        # bound customer's live session in the same transaction — epoch bump,
        # lease in the past, a LOGOUT event naming this operator and the
        # code_suspended reason (deleting only the client token never counts
        # as a server-side revocation). A never-activated code carries no
        # user and no session: nothing to propagate.
        if bound_user_id is not None:
            revoke_session(
                conn,
                user_id=str(bound_user_id),
                actor_user_id=actor.user_id,
                reason=REASON_CODE_SUSPENDED,
                request_id=request_id,
                now_iso=_transaction_now_iso(conn),
            )
        logger.info(
            "activation code suspended: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {"code_id": code_id, "status": "SUSPENDED", "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/activation-codes/{code_id}/resume")
def resume_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Resume a suspended, activated code (the matrix has no back-to-ISSUED edge)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status, bound_user_id")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        current = str(code_row[0])
        try:
            assert_code_transition(current, "ACTIVE")
        except InvalidCodeTransitionError as exc:
            raise _transition_error(current, "ACTIVE") from exc
        if code_row[1] is None:
            # A never-activated suspended code cannot resume: the frozen
            # matrix has no SUSPENDED -> ISSUED edge, so only a bound code
            # may come back to ACTIVE.
            raise _http(
                409,
                "CODE_NOT_ACTIVATED",
                "Only an activated code can be resumed.",
            )
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET status = 'ACTIVE', suspended_at = NULL WHERE id = %s",
            (code_id,),
        )
        _record_code_event(
            conn,
            code_id=code_id,
            event="RESUMED",
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
        )
        logger.info(
            "activation code resumed: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {"code_id": code_id, "status": "ACTIVE", "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/activation-codes/{code_id}/revoke")
def revoke_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Revoke a code permanently (terminal state, binding kept for audit)."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status, bound_user_id")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        current = str(code_row[0])
        try:
            assert_code_transition(current, "REVOKED")
        except InvalidCodeTransitionError as exc:
            raise _transition_error(current, "REVOKED") from exc
        bound_user_id = code_row[1]
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET status = 'REVOKED', revoked_at = %s WHERE id = %s",
            (_now_iso(), code_id),
        )
        _record_code_event(
            conn,
            code_id=code_id,
            event="REVOKED",
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
        )
        # T20 / SES-03 revocation propagation: the revocation terminates the
        # bound customer's live session in the same transaction, exactly like
        # the suspension above but with the terminal code_revoked reason.
        if bound_user_id is not None:
            revoke_session(
                conn,
                user_id=str(bound_user_id),
                actor_user_id=actor.user_id,
                reason=REASON_CODE_REVOKED,
                request_id=request_id,
                now_iso=_transaction_now_iso(conn),
            )
        logger.info(
            "activation code revoked: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {"code_id": code_id, "status": "REVOKED", "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/activation-codes/{code_id}/archive")
def archive_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Hide a revoked code from daily operations without deleting its history."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        code_row = _locked_code_status(conn, code_id, "status, archived_at")
        if code_row is None:
            raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
        if str(code_row[0]) != "REVOKED":
            raise _http(409, "CODE_NOT_REVOKED", "Only revoked activation codes can be archived.")
        if code_row[1] is not None:
            raise _http(409, "CODE_ALREADY_ARCHIVED", "This activation code is already archived.")
        archived_at = _transaction_now_iso(conn)
        reason = body.reason.strip()
        conn.execute(
            "UPDATE activation_codes SET archived_at = %s WHERE id = %s",
            (archived_at, code_id),
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'admin.activation_code.archived', 'activation_code', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                code_id,
                json.dumps(
                    {"request_id": request_id, "reason": reason},
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        )
        logger.info(
            "activation code archived: code=%s actor=%s request=%s",
            code_id,
            actor.user_id,
            request_id,
        )
        return {
            "code_id": code_id,
            "archived_at": archived_at,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


# ---------------------------------------------------------------------------
# Listing (read path)
# ---------------------------------------------------------------------------


def _recover_plaintext_codes(
    conn: psycopg.Connection,
    rows: list[tuple[object, ...]],
) -> dict[str, str]:
    """Map stored code digests to plaintext recovered from sealed exports.

    Every export for the requested batches is considered because a batch may
    be generated in more than one operation. Plaintext remains process-local
    and is never written back to PostgreSQL or emitted to logs.
    """
    batch_ids = sorted({str(row[1]) for row in rows})
    if not batch_ids:
        return {}
    aead_keys = configured_export_aead_keys()
    export_rows = conn.execute(
        "SELECT batch_id, ciphertext, key_version "
        "FROM activation_code_exports "
        "WHERE batch_id = ANY(%s) AND ciphertext IS NOT NULL "
        "ORDER BY batch_id, created_at",
        (batch_ids,),
    ).fetchall()
    recovered: dict[str, str] = {}
    for export_batch_id, ciphertext, key_version in export_rows:
        key = aead_keys.get(int(key_version))
        if key is None:
            raise ActivationKeyError(
                f"activation export key version {key_version} is not configured"
            )
        codes = decrypt_code_package(
            str(ciphertext),
            key=key,
            batch_id=str(export_batch_id),
        )
        for code in codes:
            for digest, _version in iter_code_digests(code):
                recovered[digest] = code
    return recovered


@router.post("/activation-codes/{code_id}/reveal")
def reveal_activation_code(
    code_id: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Reveal one retained code for an explicit, audited administrator copy.

    The plaintext is decrypted in memory and returned with ``no-store``. The
    idempotency snapshot contains identifiers only; it never stores the code.
    """
    idempotency_key, reason = _require_write_contract(request, body)
    route = _canonical_route(request)
    request_hash = _request_hash(route, dict(request.path_params), body)
    request_id = get_or_create_request_id(request)
    replay_attempt_request_id = request_id
    response.headers["Cache-Control"] = "no-store"
    try:
        with pg_transaction() as conn:
            placeholder = _begin_idempotent_write(
                conn,
                actor_user_id=actor.user_id,
                route=route,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            replay = placeholder is None
            if replay:
                snapshot = _load_idempotent_snapshot(
                    conn,
                    actor_user_id=actor.user_id,
                    route=route,
                    idempotency_key=idempotency_key,
                )
                if (
                    snapshot is None
                    or snapshot.request_hash != request_hash
                    or snapshot.response_status != 200
                    or snapshot.response_body is None
                ):
                    raise _http(
                        409,
                        "IDEMPOTENCY_CONFLICT",
                        "This idempotency key was already used for a different request.",
                    )
                stored = json.loads(snapshot.response_body)
                request_id = str(stored["request_id"])

            row = conn.execute(
                "SELECT id, batch_id, code_digest, masked_code, status, "
                "bound_user_id, issued_at FROM activation_codes WHERE id = %s",
                (code_id,),
            ).fetchone()
            if row is None:
                raise _http(404, "CODE_NOT_FOUND", "Unknown activation code.")
            plaintext = _recover_plaintext_codes(conn, [row]).get(str(row[2]))
            if plaintext is None:
                raise _http(
                    409,
                    "CODE_PLAINTEXT_UNAVAILABLE",
                    "The retained recovery envelope for this code is no longer available.",
                )
            safe_payload: dict[str, object] = {
                "code_id": code_id,
                "masked_code": str(row[3]),
                "request_id": request_id,
            }
            if placeholder is not None:
                conn.execute(
                    "INSERT INTO audit_logs "
                    "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
                    "VALUES (%s, %s, %s, 'activation_code', %s, %s)",
                    (
                        str(uuid.uuid4()),
                        actor.user_id,
                        "admin.activation_code.revealed",
                        code_id,
                        json.dumps(
                            {"request_id": request_id, "reason": reason},
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                    ),
                )
                _finish_idempotent_write(
                    conn,
                    placeholder,
                    response_status=200,
                    response_body=safe_payload,
                )
            else:
                conn.execute(
                    "INSERT INTO audit_logs "
                    "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
                    "VALUES (%s, %s, %s, 'activation_code', %s, %s)",
                    (
                        str(uuid.uuid4()),
                        actor.user_id,
                        "admin.activation_code.revealed_replay",
                        code_id,
                        json.dumps(
                            {
                                "original_request_id": request_id,
                                "replay_request_id": replay_attempt_request_id,
                                "reason": reason,
                            },
                            ensure_ascii=True,
                            sort_keys=True,
                        ),
                    ),
                )
            response.headers[REQUEST_ID_HEADER] = request_id
            if replay:
                response.headers[REPLAY_HEADER] = "true"
    except (ActivationKeyError, ActivationExportError) as exc:
        logger.warning(
            "activation code reveal unavailable: code=%s actor=%s error=%s",
            code_id,
            actor.user_id,
            type(exc).__name__,
        )
        raise _http(
            503,
            "ACTIVATION_DETAILS_UNAVAILABLE",
            "Activation code details are temporarily unavailable.",
        ) from exc
    except RuntimeError as exc:
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Activation code management requires the PostgreSQL runtime.",
        ) from exc
    logger.info(
        "activation code revealed: code=%s actor=%s request=%s",
        code_id,
        actor.user_id,
        request_id,
    )
    return {**safe_payload, "activation_code": plaintext}


@router.get("/activation-codes")
def list_activation_codes(
    actor: AdminReader,
    response: Response,
    batch_id: str | None = None,
    status: str | None = None,
    search: str = "",
    include_archived: bool = False,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List masked code metadata without bulk-recovering plaintext values.

    A5/A9（2026-09-02 评估）: the response carries ``total`` so the console
    paginates honestly, and ``search`` matches the masked code or bound
    username server-side (LIKE wildcards escaped) instead of the client
    filtering a fixed first page.
    """
    response.headers["Cache-Control"] = "no-store"
    bounded_limit = max(0, min(limit, MAX_LIST_LIMIT))
    bounded_offset = max(0, offset)
    clauses: list[str] = []
    params: list[object] = []
    display_status = (
        "CASE WHEN code.status IN ('GENERATED', 'ISSUED') "
        "AND batch.activation_expires_at::timestamptz <= now() "
        "THEN 'EXPIRED' ELSE code.status END"
    )
    # C7：默认隐藏已归档码；显式 opt-in 后可回看（归档只隐藏，不删史）。
    if not include_archived:
        clauses.append("code.archived_at IS NULL")
    if batch_id:
        clauses.append("code.batch_id = %s")
        params.append(batch_id)
    if status:
        clauses.append(f"({display_status}) = %s")
        params.append(status)
    if search.strip():
        literal = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(code.masked_code ILIKE %s OR customer.username ILIKE %s)")
        params.extend((f"%{literal}%", f"%{literal}%"))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with pg_transaction() as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) FROM activation_codes AS code "
                f"JOIN activation_code_batches AS batch ON batch.id = code.batch_id "
                f"LEFT JOIN users AS customer ON customer.id = code.bound_user_id "
                f"{where}",
                params,
            ).fetchone()
            total = int(total_row[0]) if total_row is not None else 0
            rows = conn.execute(
                f"SELECT code.id, code.batch_id, code.masked_code, ({display_status}), "
                f"code.bound_user_id, code.issued_at, customer.username, "
                f"code.archived_at, batch.activation_expires_at, "
                f"(SELECT MIN(event.created_at) FROM activation_code_events event "
                f"WHERE event.code_id = code.id AND event.event = 'GENERATED') "
                f"FROM activation_codes AS code "
                f"JOIN activation_code_batches AS batch ON batch.id = code.batch_id "
                f"LEFT JOIN users AS customer ON customer.id = code.bound_user_id "
                f"{where} ORDER BY code.id LIMIT %s OFFSET %s",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            code_ids = [str(row[0]) for row in rows]
            device_rows = (
                conn.execute(
                    "SELECT activation_code_id, id, slot_no, display_name, platform, "
                    "status, bound_at, last_active_at, unbound_at, revoked_at "
                    "FROM customer_devices WHERE activation_code_id = ANY(%s) "
                    "ORDER BY activation_code_id, slot_no, bound_at",
                    (code_ids,),
                ).fetchall()
                if code_ids
                else []
            )
            pairing_rows = (
                conn.execute(
                    "SELECT activation_code_id, id, display_name, platform, status, "
                    "created_at, expires_at FROM device_pairing_requests "
                    "WHERE activation_code_id = ANY(%s) "
                    "AND status IN ('PENDING', 'APPROVED') "
                    "AND expires_at::timestamptz > now() "
                    "ORDER BY activation_code_id, created_at, id",
                    (code_ids,),
                ).fetchall()
                if code_ids
                else []
            )
    except RuntimeError as exc:
        raise _http(
            503,
            "ACTIVATION_SERVICE_UNAVAILABLE",
            "Activation code management requires the PostgreSQL runtime.",
        ) from exc
    devices_by_code: dict[str, list[dict[str, object]]] = {}
    for device in device_rows:
        devices_by_code.setdefault(str(device[0]), []).append(
            {
                "device_id": str(device[1]),
                "slot_no": int(device[2]),
                "display_name": device[3],
                "platform": str(device[4]),
                "status": str(device[5]),
                "bound_at": device[6],
                "last_active_at": device[7],
                "unbound_at": device[8],
                "revoked_at": device[9],
            }
        )
    pairings_by_code: dict[str, list[dict[str, object]]] = {}
    for pairing in pairing_rows:
        pairings_by_code.setdefault(str(pairing[0]), []).append(
            {
                "pairing_request_id": str(pairing[1]),
                "display_name": str(pairing[2]),
                "platform": str(pairing[3]),
                "status": str(pairing[4]),
                "created_at": pairing[5],
                "expires_at": pairing[6],
            }
        )
    items = [
        {
            "code_id": str(row[0]),
            "batch_id": str(row[1]),
            "masked_code": str(row[2]),
            "status": str(row[3]),
            "bound_user_id": row[4],
            "issued_at": row[5],
            "bound_username": row[6],
            "archived_at": row[7],
            "expires_at": row[8],
            "created_at": row[9],
            "devices": devices_by_code.get(str(row[0]), []),
            "pending_pairings": pairings_by_code.get(str(row[0]), []),
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }
