"""T21 / SES-04 — the customer session fencing wiring (dev doc §12.4, plan B).

The early-snapshot + in-transaction re-verification pair every customer write
route uses:

- ``CustomerSessionSnapshot`` + ``customer_session_snapshot`` — the FastAPI
  dependency that resolves the presented session token to the expected
  session context at request time. It is an *early* 401 gate only; the final
  verdict is always the in-transaction ``verify_session_context`` inside
  ``fenced_pg_transaction`` (the code-checklist §9.2 / SES-04 red line: the
  dependency alone never closes the task). Returns ``None`` when no
  PostgreSQL runtime is configured — the internal/desktop lane has no
  customer sessions and authenticates through ``AuthenticatedUser`` instead.
- ``fenced_pg_transaction`` — the customer session-verification transaction
  primitive: opens ``pg_transaction()``, re-verifies the session under the row
  lock inside it (epoch/device/session/lease re-compared, code/device status
  re-checked, lease judged on ``clock_timestamp()``), and yields ``(conn,
  ctx)``. Business writes opt into same-transaction commit evidence; read-only
  callers do not. Any business exception rolls the whole transaction back; a
  ``SessionFencingError`` answers 401 — a request that passed the snapshot can
  never commit a write after a switch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated

import psycopg
from fastapi import Depends, HTTPException, Request

from app import ops_metrics
from app.activation_code_service import ActivationKeyError
from app.api_key_service import (
    ApiKeyError,
    authenticate_api_key,
    parse_api_key_prefix,
    touch_last_used,
)
from app.auth import CurrentUser
from app.auth_headers import bearer_token as _bearer_token
from app.customer_auth import (
    CustomerSessionContext,
    SessionFencingError,
    verify_session_context,
)
from app.customer_device_service import _token_digests
from app.db_pg import (
    DATABASE_URL_ENV,
    SQLITE_URL_SCHEMES,
    IsolationLevel,
    get_pg_pool,
    pg_transaction,
)
from app.db_portable import BusinessConnection
from app.permissions import AuditedSecurityDenial, persist_security_denial
from app.security_rate_limit import (
    DIMENSION_APIKEY_IP,
    DIMENSION_APIKEY_KEY,
    RateLimitDecision,
    apikey_ip_limit,
    apikey_key_limit,
    client_ip_from_request,
    consume_rate_limit,
    rate_limit_window_seconds,
    record_auth_failure,
)

FENCING_FAILURE_DIMENSION = "session:fencing"
# CW-078: the API-Key lane is detected by this fixed plaintext scheme prefix, so
# get_business_db / customer_read_transaction route an ``xsk_live_`` bearer to
# the independent lane before the session fence ever sees it (§2.5 B).
API_KEY_BEARER_PREFIX = "xsk_live_"
RETRY_AFTER_HEADER = "Retry-After"
logger = logging.getLogger(__name__)


def _customer_database_configured() -> bool:
    """Choose the lane from current configuration, never from a cached pool."""
    url = os.environ.get(DATABASE_URL_ENV, "").strip()
    return bool(url) and not url.startswith(SQLITE_URL_SCHEMES)


@dataclass(frozen=True)
class CustomerSessionSnapshot:
    """The expected session context captured at request time (early, unlocked).

    Mirrors the §12.4 re-comparison tuple (user / device / session / epoch /
    lease) so ``fenced_pg_transaction`` can re-check every field inside the
    business transaction. ``token`` is the presented session token; the raw
    value is never written anywhere.
    """

    token: str
    expected_user_id: str
    expected_device_id: str
    expected_session_id: str
    expected_session_epoch: int
    expected_lease_until: str


def customer_session_snapshot(request: Request) -> CustomerSessionSnapshot | None:
    """Resolve the presented Bearer session token to the expected context.

    Early and non-authoritative: the read carries no row lock and the 401 here
    is only a fast gate. Returns ``None`` when no PostgreSQL runtime is
    configured (internal/desktop lane — the caller authenticates through
    ``AuthenticatedUser``). Raises 401 for a missing/unknown/replaced token and
    503 fail-closed when the device-domain keys are misconfigured (a server
    outage must never masquerade as a client credential problem).
    """
    if not _customer_database_configured():
        # No PostgreSQL runtime — the internal/desktop lane has no customer
        # sessions; the caller uses the internal identity instead.
        return None
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            503,
            detail={
                "code": "SESSION_SERVICE_UNAVAILABLE",
                "message": "The customer session service is unavailable.",
            },
        ) from exc
    token = _bearer_token(request)
    if token is None:
        # PostgreSQL is configured: this is the customer lane, where the
        # session token is the only identity. A missing token must not fall
        # through to the internal lane.
        raise HTTPException(
            401,
            detail={
                "code": "SESSION_TOKEN_REQUIRED",
                "message": "A Bearer session token is required on the customer lane.",
            },
        )
    try:
        digests = _token_digests(token)
    except ActivationKeyError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "SESSION_SERVICE_UNAVAILABLE",
                "message": "Session keys are not configured; customer sessions are refused.",
            },
        ) from exc
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT user_id, device_id, session_id, session_epoch, lease_until "
            "FROM customer_session_state WHERE token_digest = ANY(%s) LIMIT 1",
            (digests,),
        ).fetchone()
    if row is None:
        raise HTTPException(
            401,
            detail={
                "code": "SESSION_REPLACED",
                "message": "This session token no longer owns a live session.",
            },
        )
    ops_metrics.set_current_trace_fields(
        user_id=str(row[0]),
        device_id=str(row[1]),
        session_id=str(row[2]),
        session_epoch=int(row[3]),
    )
    return CustomerSessionSnapshot(
        token=token,
        expected_user_id=str(row[0]),
        expected_device_id=str(row[1]),
        expected_session_id=str(row[2]),
        expected_session_epoch=int(row[3]),
        expected_lease_until=str(row[4]),
    )


@contextmanager
def fenced_pg_transaction(
    snapshot: CustomerSessionSnapshot,
    *,
    isolation: IsolationLevel | None = None,
    record_write_evidence: bool = False,
) -> Iterator[tuple[psycopg.Connection, CustomerSessionContext]]:
    """A customer fenced transaction; write callers opt into commit evidence.

    Opens the PostgreSQL transaction, runs ``verify_session_context`` under the
    session-row lock inside it (the full §12.4 re-comparison plus the code and
    device status re-check and the ``clock_timestamp()`` lease verdict), then
    yields ``(conn, ctx)``. A business exception rolls the transaction back;
    a ``SessionFencingError`` raises 401. Business-write owners must pass
    ``record_write_evidence=True``; read-only routes leave it false.
    """
    ops_metrics.set_current_trace_fields(
        user_id=snapshot.expected_user_id,
        device_id=snapshot.expected_device_id,
        session_id=snapshot.expected_session_id,
        session_epoch=snapshot.expected_session_epoch,
    )
    started = time.perf_counter()
    try:
        with pg_transaction(isolation=isolation) as conn:
            ctx = verify_session_context(
                conn,
                presentation_session_token=snapshot.token,
                expected_user_id=snapshot.expected_user_id,
                expected_device_id=snapshot.expected_device_id,
                expected_session_id=snapshot.expected_session_id,
                expected_session_epoch=snapshot.expected_session_epoch,
                expected_lease_until=snapshot.expected_lease_until,
            )
            _observe_fencing_lock_wait(time.perf_counter() - started)
            yield conn, ctx
            if record_write_evidence:
                _record_fencing_commit_evidence(conn, snapshot, ctx)
    except AuditedSecurityDenial as exc:
        # The pg_transaction context has already rolled back the business
        # transaction. Persist the denial separately so the P1 probe sees it.
        persist_security_denial(exc)
        raise
    except SessionFencingError as exc:
        lock_wait_seconds = time.perf_counter() - started
        _record_fencing_reject(exc.code, lock_wait_seconds)
        ops_metrics.set_current_result_code(exc.code)
        try:
            _record_fencing_failure_audit(snapshot)
        except Exception as audit_error:
            logger.warning(
                "fencing failure audit hook unavailable (%s)",
                type(audit_error).__name__,
            )
        try:
            _log_fencing_reject_audit(exc.code, lock_wait_seconds)
        except Exception as audit_error:
            logger.warning(
                "fencing reject audit hook unavailable (%s)",
                type(audit_error).__name__,
            )
        raise HTTPException(
            401,
            detail={"code": exc.code, "message": exc.message},
        ) from None


def _observe_fencing_lock_wait(lock_wait_seconds: float) -> None:
    try:
        ops_metrics.observe_fencing_lock_wait(lock_wait_seconds=lock_wait_seconds)
    except Exception as metrics_error:
        logger.warning(
            "fencing lock wait metrics unavailable (%s)",
            type(metrics_error).__name__,
        )


def _record_fencing_reject(code: str, lock_wait_seconds: float) -> None:
    try:
        ops_metrics.record_fencing_reject(code=code, lock_wait_seconds=lock_wait_seconds)
    except Exception as metrics_error:
        logger.warning(
            "fencing reject metrics unavailable (%s)",
            type(metrics_error).__name__,
        )


def _log_fencing_reject_audit(code: str, lock_wait_seconds: float) -> None:
    context = ops_metrics.current_request_context()
    payload = {
        "event": "customer_session_fenced",
        "request_id": context.request_id if context is not None else "-",
        "method": context.method if context is not None else "-",
        "route": ops_metrics.current_route_label(),
        "result_code": code,
        "lock_wait_ms": max(int(lock_wait_seconds * 1000), 0),
    }
    try:
        logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True))
    except Exception as audit_error:
        logger.warning(
            "fencing reject audit unavailable (%s)",
            type(audit_error).__name__,
        )


def _record_fencing_failure_audit(snapshot: CustomerSessionSnapshot) -> None:
    context = ops_metrics.current_request_context()
    request_id = context.request_id if context is not None else None
    try:
        with pg_transaction() as conn:
            record_auth_failure(
                conn,
                dimension=FENCING_FAILURE_DIMENSION,
                identifier=_fencing_failure_identifier(snapshot),
                request_id=request_id,
                dedupe_window_seconds=rate_limit_window_seconds(),
            )
    except Exception as audit_error:
        logger.warning(
            "fencing failure audit row unavailable (%s)",
            type(audit_error).__name__,
        )


def _fencing_failure_identifier(snapshot: CustomerSessionSnapshot) -> str:
    payload = "|".join(
        (
            snapshot.expected_user_id,
            snapshot.expected_device_id,
            snapshot.expected_session_id,
            str(snapshot.expected_session_epoch),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_fencing_commit_evidence(
    conn: psycopg.Connection,
    snapshot: CustomerSessionSnapshot,
    verified: CustomerSessionContext,
) -> None:
    """Persist one bounded epoch-pair fact only when the business write commits.

    The row is inserted after the route body succeeds but before the outer PG
    transaction commits. A route error or commit failure rolls it back with the
    business write. Repeated writes in one session pair deduplicate, while a
    regressed verifier returning a different epoch creates a durable P1 fact.
    """
    context = ops_metrics.current_request_context()
    request_id = context.request_id if context is not None else str(uuid.uuid4())
    conn.execute(
        "INSERT INTO customer_fencing_write_evidence "
        "(id, request_id, subject_digest, expected_session_epoch, "
        "verified_session_epoch) VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (subject_digest, expected_session_epoch, verified_session_epoch) "
        "DO NOTHING",
        (
            str(uuid.uuid4()),
            request_id,
            _fencing_failure_identifier(snapshot),
            snapshot.expected_session_epoch,
            verified.session_epoch,
        ),
    )


# ---------------------------------------------------------------------------
# CW-078 — the API-Key lane (§2.5 B): an independent customer credential that
# never passes through the session fence. get_business_db / customer_read_
# transaction detect an ``xsk_live_`` bearer and route it here; api_key_service
# resolves the credential (prefix lookup + versioned HMAC-SHA256 compare) and
# business writes run in a plain pg_transaction (no fenced_pg_transaction).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApiKeyUser:
    """The API-Key lane principal: an authenticated customer program.

    Independent of the session fence (§2.5 B) — it carries the key id (for the
    ``last_used_at`` writeback) and the owning customer's user id, plus the
    stored scopes. There is deliberately no device / session / epoch / lease:
    those are the fence's anchors, and §2.5 B documents the accepted risks of
    having none (no device binding, no preemption, no lease expiry), mitigated
    by one-time plaintext, the ``apikey:*`` budget and revoke-on-compromise.
    """

    key_id: str
    user_id: str
    scopes: tuple[str, ...]
    required_scope: str | None = None


def _api_key_bearer(request: Request) -> str | None:
    """The presented plaintext key when the bearer is an ``xsk_live_`` credential."""
    token = _bearer_token(request)
    if token is not None and token.startswith(API_KEY_BEARER_PREFIX):
        return token
    return None


def _api_key_failure_identifier(prefix: str) -> str:
    """The ``apikey:key`` audit identifier: a digest of the prefix, never the key.

    The 40-char secret never reaches ``security_auth_failures`` (R-A / the
    ``activate:code`` precedent); the 8-char prefix is digested so a hammered
    candidate stays countable without storing anything replayable.
    """
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def _enforce_api_key_failure_budget(request: Request, plaintext: str) -> None:
    """Burn the API-Key brute-force budget and append the failure audit trail.

    Runs in its own committed transaction so the rolled-back authentication
    lookup never refunds it (the activation / fencing precedent). Best-effort: a
    flaky audit write must not turn a legal 401 into a 500, so it logs and
    swallows its own failure. When the address budget is already exhausted the
    caller's 401 is upgraded to a 429 carrying ``Retry-After``.
    """
    context = ops_metrics.current_request_context()
    request_id = context.request_id if context is not None else None
    client_ip = client_ip_from_request(request)
    prefix = parse_api_key_prefix(plaintext)
    window = rate_limit_window_seconds()
    try:
        with pg_transaction() as conn:
            ip_decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_APIKEY_IP,
                identifier=client_ip,
                limit=apikey_ip_limit(),
                window_seconds=window,
            )
            key_decision: RateLimitDecision | None = None
            # A blocked address draws no per-key budget (PR #46 precedent): the
            # key identifier is attacker-controlled, so a blocked client must
            # not mint one unbounded counter row per random well-shaped key.
            if prefix is not None and ip_decision.allowed:
                key_decision = consume_rate_limit(
                    conn,
                    dimension=DIMENSION_APIKEY_KEY,
                    identifier=_api_key_failure_identifier(prefix),
                    limit=apikey_key_limit(),
                    window_seconds=window,
                )
            record_auth_failure(
                conn,
                dimension=DIMENSION_APIKEY_IP,
                identifier=client_ip,
                request_id=request_id,
                dedupe_window_seconds=window,
            )
            if prefix is not None:
                record_auth_failure(
                    conn,
                    dimension=DIMENSION_APIKEY_KEY,
                    identifier=_api_key_failure_identifier(prefix),
                    request_id=request_id,
                    dedupe_window_seconds=window,
                )
    except Exception as audit_error:
        logger.warning(
            "api key failure budget unavailable (%s)",
            type(audit_error).__name__,
        )
        return
    if not ip_decision.allowed or (key_decision is not None and not key_decision.allowed):
        retry_after = max(
            ip_decision.retry_after_seconds,
            key_decision.retry_after_seconds if key_decision is not None else 0,
        )
        blocked = HTTPException(
            429,
            detail={
                "code": "API_KEY_RATE_LIMITED",
                "message": "Too many API-key attempts; retry later.",
            },
        )
        blocked.headers = {RETRY_AFTER_HEADER: str(retry_after)}
        raise blocked


def resolve_api_key_user(request: Request) -> ApiKeyUser | None:
    """Resolve an ``xsk_live_`` bearer to its ApiKeyUser, or None if not a key.

    The independent lane's early gate (§2.5 B): it never touches
    ``customer_session_snapshot`` or the fence. Returns None when the request
    carries no API-Key-scheme bearer, so the caller falls through to the session
    / internal lane. A malformed, unknown, mismatched or revoked key is the
    single 401 ``API_KEY_INVALID`` (no oracle) after burning the ``apikey:*``
    budget; a missing HMAC-key configuration or an unavailable pool is a 503
    fail-closed — a server outage must never masquerade as a client credential
    problem.
    """
    plaintext = _api_key_bearer(request)
    if plaintext is None:
        return None
    if not _customer_database_configured():
        # API Keys are customer-PG infrastructure; the internal/desktop lane has
        # no customer_api_keys table, so such a bearer can never be valid.
        raise HTTPException(
            401,
            detail={
                "code": "API_KEY_INVALID",
                "message": "The API key is not valid or has been revoked.",
            },
        )
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(
            503,
            detail={
                "code": "API_KEY_SERVICE_UNAVAILABLE",
                "message": "The API-key service is unavailable.",
            },
        ) from exc
    try:
        with pg_transaction() as conn:
            authed = authenticate_api_key(conn, plaintext)
    except ApiKeyError as exc:
        raise HTTPException(
            503,
            detail={
                "code": "API_KEY_SERVICE_UNAVAILABLE",
                "message": "API-key digest keys are not configured; the lane is refused.",
            },
        ) from exc
    if authed is None:
        _enforce_api_key_failure_budget(request, plaintext)
        raise HTTPException(
            401,
            detail={
                "code": "API_KEY_INVALID",
                "message": "The API key is not valid or has been revoked.",
            },
        )
    ops_metrics.set_current_trace_fields(user_id=authed.user_id)
    path = request.url.path.rstrip("/")
    required_scope = None
    generation_reads = (
        r"/api/generation/(price-quote|runtime-limits)",
        r"/api/generation-batches(?:/[^/]+)?",
        r"/api/independent/capabilities",
        r"/api/oral/(price|tasks(?:/[^/]+)?)",
    )
    generation_writes = (
        r"/api/projects/[^/]+/generation-batches",
        r"/api/generation-batches/[^/]+/(cancel|regenerate)",
        r"/api/generation-tasks/[^/]+/(retry|regenerate|cancel|archive-retry)",
        r"/api/independent/video-tasks",
        r"/api/oral/tasks(?:/[^/]+/(cancel|archive-retry))?",
    )
    if (
        request.method == "GET" and any(re.fullmatch(pattern, path) for pattern in generation_reads)
    ) or (
        request.method == "POST"
        and any(re.fullmatch(pattern, path) for pattern in generation_writes)
    ):
        required_scope = "generation"
    if path == "/api/customer/pricing" and request.method == "GET":
        required_scope = "pricing"
    if path == "/api/customer/wallet" or path == "/api/customer/wallet/transactions":
        if request.method == "GET":
            required_scope = "wallet"
    elif path == "/api/customer/recharge-orders":
        if request.method in {"GET", "POST"}:
            required_scope = "recharge"
    elif path.startswith("/api/customer/recharge-orders/") and request.method == "GET":
        required_scope = "recharge"
    elif path == "/api/customer/recharge-packages" and request.method == "GET":
        # 套餐是充值页的展示数据：与额度/下单同属 recharge scope，不单独发新 scope。
        required_scope = "recharge"
    if required_scope is None or required_scope not in authed.scopes:
        raise HTTPException(
            403, detail={"code": "API_KEY_SCOPE_DENIED", "message": "Token 无权执行此操作。"}
        )
    return ApiKeyUser(
        key_id=authed.key_id,
        user_id=authed.user_id,
        scopes=authed.scopes,
        required_scope=required_scope,
    )


def _verify_api_key_in_transaction(conn: psycopg.Connection, principal: ApiKeyUser) -> None:
    """Lock owner then credential, matching management lock order until commit.

    A revoke or account disable committed after the early lookup cannot enter
    business work. Concurrent changes wait for already accepted work to finish.

    The lane honours the same authority chain as the session fence
    (``verify_session_context``): a deactivated master fences every credential
    riding under it, and a suspended/revoked activation code fences the account
    it authenticates. Without the parent and code checks a banned master's
    sub-account keys would keep charging the frozen wallet.

    The code check is deliberately account-wide (any BOUND device on a
    non-ACTIVE code denies), which is stricter than the session fence's
    per-session code check. Today the two are equivalent because
    ``uq_activation_codes_bound_user_current`` pins one live code per user;
    if that invariant is ever relaxed (multi-code accounts), revisit this
    predicate — a stale BOUND row from a revoked code would otherwise lock
    a legitimate account out of every API key.
    """
    owner = conn.execute(
        "SELECT u.is_active, u.role, "
        "(u.parent_user_id IS NULL OR p.is_active = 1) AS parent_ok, "
        "NOT EXISTS ("
        " SELECT 1 FROM customer_devices d"
        " JOIN activation_codes c ON c.id = d.activation_code_id"
        " WHERE d.user_id = u.id AND d.status = 'BOUND' AND c.status <> 'ACTIVE'"
        ") AS codes_ok "
        "FROM users u LEFT JOIN users p ON p.id = u.parent_user_id "
        "WHERE u.id = %s FOR SHARE OF u",
        (principal.user_id,),
    ).fetchone()
    key = conn.execute(
        "SELECT revoked_at, scopes FROM customer_api_keys "
        "WHERE id = %s AND user_id = %s FOR UPDATE",
        (principal.key_id, principal.user_id),
    ).fetchone()
    if (
        owner is None
        or not owner[0]
        or owner[1] != "customer"
        or not owner[2]
        or not owner[3]
        or key is None
        or key[0]
    ):
        # 授权链被拒（母账号封禁 / 激活码吊销）时留一条可诊断的 warn：
        # 对外仍是单一失败答案 API_KEY_INVALID，但「API 客户端批量 401」
        # 的报障需要服务端证据（key 自身吊销走上面的通用分支，不在此列）。
        if (
            owner is not None
            and owner[0]
            and owner[1] == "customer"
            and (not owner[2] or not owner[3])
        ):
            logger.warning(
                "api-key lane denied by authority chain: user_id=%s parent_ok=%s codes_ok=%s",
                principal.user_id,
                owner[2],
                owner[3],
            )
        raise HTTPException(
            401, detail={"code": "API_KEY_INVALID", "message": "Token 已失效或账号不可用。"}
        )
    if principal.required_scope and principal.required_scope not in json.loads(key[1]):
        raise HTTPException(
            403, detail={"code": "API_KEY_SCOPE_DENIED", "message": "Token 权限已变更。"}
        )


@contextmanager
def customer_read_transaction(
    request: Request,
) -> Iterator[tuple[psycopg.Connection, str]]:
    """Unified customer-lane read for the API-Key whitelist endpoints (§2.2).

    Accepts EITHER an ``xsk_live_`` API-Key bearer (the independent lane: a
    plain PG transaction, no fence, ``last_used_at`` written back) OR a session
    token (the fenced lane: ``customer_session_snapshot`` +
    ``fenced_pg_transaction``, re-verified under the row lock). Yields
    ``(conn, user_id)`` so a whitelisted GET serves both credentials with one
    body. The API-Key path never calls ``fenced_pg_transaction`` (§2.5 B).
    """
    api_key = resolve_api_key_user(request)
    if api_key is not None:
        with pg_transaction() as conn:
            _verify_api_key_in_transaction(conn, api_key)
            touch_last_used(conn, key_id=api_key.key_id)
            yield conn, api_key.user_id
        return
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        yield conn, ctx.user_id


@dataclass(frozen=True)
class BusinessDb:
    """The single dependency migrated business write routes use (plan B.3).

    ``write()`` dispatches by deployment mode: the customer lane opens the
    write inside ``fenced_pg_transaction`` (the session is re-verified under
    the row lock and the actor is the session's customer), while the
    internal/desktop lane opens a fresh SQLite connection, resolves the
    internal ``AuthenticatedUser`` and yields without fencing.
    """

    snapshot: CustomerSessionSnapshot | None
    authorization: str | None
    dev_user_id: str | None
    # CW-078: set when get_business_db resolved an ``xsk_live_`` bearer to an
    # authenticated API-Key principal; write() then takes the independent lane
    # (write_for_api_key) instead of the session fence (§2.5 B).
    api_key: ApiKeyUser | None = None

    @contextmanager
    def write(
        self,
        *,
        isolation: IsolationLevel | None = None,
    ) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        """One customer business-write transaction + its acting user."""
        if self.snapshot is not None:
            with fenced_pg_transaction(
                self.snapshot,
                isolation=isolation,
                record_write_evidence=True,
            ) as (conn, ctx):
                bc = BusinessConnection.postgres(conn)
                bc.ctx = ctx
                bc.auth_source = "session"
                actor = CurrentUser(
                    id=ctx.user_id,
                    username=ctx.user_id,
                    display_name=ctx.user_id,
                    role="customer",
                )
                yield bc, actor
            return
        if self.api_key is not None:
            with self.write_for_api_key(isolation=isolation) as (bc, actor):
                yield bc, actor
            return
        # CW-042-b: internal lane 直接读取 DB_PATH_ENV 的通道已随 SQLite lane
        # 退役（下方 raise）；customer lane（DATABASE_URL_ENV=postgresql://）
        # + snapshot=None 时，
        # 不能回退到 SQLite，必须抛 503（SES-04 红线：PG writer 必须有 customer session）。
        url = os.environ.get(DATABASE_URL_ENV, "").strip()
        if url and not url.startswith(SQLITE_URL_SCHEMES):
            raise HTTPException(
                503,
                detail={
                    "code": "DATABASE_NOT_CONFIGURED",
                    "message": "A PostgreSQL writer requires a customer session snapshot.",
                },
            )
        raise HTTPException(
            503,
            detail={
                "code": "DATABASE_NOT_CONFIGURED",
                "message": "The internal SQLite lane is retired (CW-042-b); "
                "configure VIDEO_REPLICA_DATABASE_URL.",
            },
        )

    @contextmanager
    def write_for_api_key(
        self,
        *,
        isolation: IsolationLevel | None = None,
    ) -> Iterator[tuple[BusinessConnection, CurrentUser]]:
        """The API-Key lane business write (§2.5 B): an independent PG
        transaction that never calls ``fenced_pg_transaction``.

        ``last_used_at`` is written back as the authentication side effect and
        the owning customer is the acting user. There is no session to
        verify; the owning account and credential are rechecked under row locks
        before work is accepted. Any additional row lock a whitelisted write
        needs (e.g. ``FOR UPDATE`` on a wallet row) is the caller's to take
        inside the yielded connection, exactly as on the session lane. A
        business exception rolls the whole transaction back.
        """
        assert self.api_key is not None
        with pg_transaction(isolation=isolation) as conn:
            _verify_api_key_in_transaction(conn, self.api_key)
            touch_last_used(conn, key_id=self.api_key.key_id)
            bc = BusinessConnection.postgres(conn)
            bc.api_key_id = self.api_key.key_id
            bc.auth_source = "api_key"
            actor = CurrentUser(
                id=self.api_key.user_id,
                username=self.api_key.user_id,
                display_name=self.api_key.user_id,
                role="customer",
            )
            yield bc, actor


def get_business_db(request: Request) -> BusinessDb:
    """FastAPI dependency: the customer-business write entry for migrated routes.

    Three lanes resolve here (§2.2 认证三轨). An ``xsk_live_`` bearer is taken
    first: ``resolve_api_key_user`` authenticates it on the independent API-Key
    lane (no fence) and ``write()`` then dispatches to ``write_for_api_key``.
    Otherwise, on the customer lane the session snapshot is taken here (the
    early 401 gate); the final verdict always happens in
    ``fenced_pg_transaction`` inside ``write()`` (the SES-04 red line). With
    neither, the internal/desktop lane resolves ``AuthenticatedUser``.
    """
    api_key = resolve_api_key_user(request)
    if api_key is not None:
        return BusinessDb(snapshot=None, authorization=None, dev_user_id=None, api_key=api_key)
    snapshot = customer_session_snapshot(request)
    if snapshot is not None:
        return BusinessDb(snapshot=snapshot, authorization=None, dev_user_id=None)
    return BusinessDb(
        snapshot=None,
        authorization=request.headers.get("Authorization"),
        dev_user_id=request.headers.get("X-Dev-User-Id"),
    )


def get_business_read_conn() -> Iterator[BusinessConnection]:
    """A mode-resolved request-scoped connection for read-side dependencies
    (storage / image provider / character storage). On the customer lane the
    fenced transaction's PostgreSQL connection is reused; on the internal lane
    a SQLite connection from the env path. The storage/provider dependencies
    must never resolve the legacy ``get_database`` independently — that opens
    only the SQLite path and 503s on a PG-only production (PR #56 P1)."""
    if not _customer_database_configured():
        # CW-025: internal lane 直接从 DB_PATH_ENV 读取，不经过 resolve_database_config()。
        # resolve_database_config() 全环境 fail-closed 后不再支持 SQLite/DB_PATH；
        # 内部 P0 遗留逻辑（internal/desktop lane）保留 DB_PATH 通道，
        # 归 CW-030/CW-040 后续处理。
        raise HTTPException(
            503,
            detail={
                "code": "DATABASE_NOT_CONFIGURED",
                "message": "The internal SQLite lane is retired (CW-042-b); "
                "configure VIDEO_REPLICA_DATABASE_URL.",
            },
        )
    with pg_transaction() as conn:
        yield BusinessConnection.postgres(conn)


BusinessReadConn = Annotated[BusinessConnection, Depends(get_business_read_conn)]


BusinessDbDep = Annotated[BusinessDb, Depends(get_business_db)]
