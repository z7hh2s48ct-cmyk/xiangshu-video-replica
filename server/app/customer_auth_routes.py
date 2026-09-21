"""CW-076 — customer self-service registration.

``POST /api/customer/register`` creates a customer identity from a username
and password with NO email/phone verification (design doc Phase 4 · CW-071).
The user row (``role='customer'``, a salted scrypt password hash, and
``registration_source='self_register'``) and its wallet are created atomically
in ONE PostgreSQL transaction, so registration can never yield a
password-less or wallet-less half-account.

Security boundaries:
- The password is hashed at the boundary (``app.password_hashing``); neither
  the plaintext nor the hash is ever returned, logged or echoed in an error.
- The username is a public identity: a strict character allow-list plus a
  length floor/ceiling, normalized (trimmed) before the uniqueness check.
- PostgreSQL is the customer source of truth (CW-025): without a PG runtime
  the route fails closed with 503 — the SQLite internal lane never
  self-registers customers.

Out of scope for CW-076 (deliberately): login, failure lockout and session
issuance are CW-077; API-key credentialing is CW-078. This endpoint only
creates the credential + wallet and returns the public identity.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api_errors import http_error as _http
from app.bootstrap import customer_public_origin, is_customer_production
from app.db_pg import get_pg_pool, pg_transaction
from app.password_hashing import PasswordPolicyError, hash_password, verify_password
from app.sub_account_auth import password_login_account_ok

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/customer", tags=["customer-auth"])

# Fixed public dummy credential: unknown accounts take the same scrypt path.
_DUMMY_PASSWORD_HASH = hash_password("cw077-public-timing-padding")

REGISTER_PATH = "/api/customer/register"

# The unique index backing ``users.username`` (revision 001). A collision on
# this constraint is the only expected race and is answered 409, never 500.
USERS_USERNAME_CONSTRAINT = "users_username_key"

REGISTRATION_SOURCE_SELF = "self_register"
_CUSTOMER_ROLE = "customer"

MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 32
# Public identity: letters, digits, dot, dash, underscore. No whitespace and
# no '@' — this lane has no email verification, so an email-shaped username
# would falsely imply one.
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class CustomerRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Bounds are generous here so the business layer (not a 422) owns the
    # precise username/password policy and its stable error codes.
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class CustomerRegistrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    username: str
    display_name: str


def _normalize_username(raw: str) -> str:
    """Trim and validate the public username, or raise the stable 400 code."""
    username = raw.strip()
    if not MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH:
        raise _http(
            400,
            "INVALID_USERNAME",
            f"username must contain {MIN_USERNAME_LENGTH} to {MAX_USERNAME_LENGTH} characters",
        )
    if not _USERNAME_PATTERN.match(username):
        raise _http(
            400,
            "INVALID_USERNAME",
            "username may only contain letters, digits, dot, dash and underscore",
        )
    return username


@router.post("/register", response_model=CustomerRegistrationResponse, status_code=201)
def register_customer(
    body: CustomerRegistrationRequest, request: Request
) -> CustomerRegistrationResponse:
    """Create one customer account (users + wallets) from username + password."""
    username = _normalize_username(body.username)

    # Fail closed before doing any work when the PG runtime is unavailable: the
    # customer edition is PostgreSQL-only, so registration cannot proceed on the
    # SQLite internal lane (mirrors the activation route's 503 guard).
    try:
        get_pg_pool()
    except (RuntimeError, ValueError) as exc:
        raise _http(
            503,
            "REGISTRATION_SERVICE_UNAVAILABLE",
            "Customer registration requires the PostgreSQL runtime.",
        ) from exc

    # Hash BEFORE opening the transaction: scrypt is deliberately CPU/memory
    # heavy, and holding a pooled connection across it would shrink the
    # effective pool under load.
    try:
        _registration_budget(request)
        password_hash = hash_password(body.password)
    except PasswordPolicyError as exc:
        raise _http(400, "WEAK_PASSWORD", str(exc)) from exc

    user_id = str(uuid.uuid4())
    try:
        with pg_transaction() as conn:
            conn.execute(
                "INSERT INTO users "
                "(id, username, display_name, role, password_hash, registration_source) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    user_id,
                    username,
                    username,
                    _CUSTOMER_ROLE,
                    password_hash,
                    REGISTRATION_SOURCE_SELF,
                ),
            )
            # The wallet starts at zero credit; recharge (CW-066/067 lane) tops
            # it up. Creating it in the same transaction is what makes the
            # account whole — a customer without a wallet row cannot reserve or
            # spend, so a partial commit would be a broken account.
            conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (user_id,))
    except UniqueViolation as exc:
        constraint = exc.diag.constraint_name or ""
        if constraint == USERS_USERNAME_CONSTRAINT:
            raise _http(409, "USERNAME_TAKEN", "That username is already registered.") from exc
        # Any other unique violation is unexpected: let it surface as a 500
        # rather than masking it as a username conflict.
        raise

    return CustomerRegistrationResponse(user_id=user_id, username=username, display_name=username)


class CustomerPasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    device_fingerprint: str = Field(min_length=16, max_length=128)
    device_platform: str = Field(default="web", min_length=1, max_length=32)
    takeover: bool = False


class CustomerPasswordLoginResponse(CustomerRegistrationResponse):
    device_id: str
    device_token: str
    session_id: str
    session_token: str
    session_epoch: int
    session_lease_expires_at: str
    # CW-062 sub-account identity: a master answers 'MASTER' with no parent; a
    # sub-account carries its master's id and display name so the desktop
    # header can badge the session without a second round-trip. The defaults
    # keep pre-sub-account sealed idempotency envelopes replayable.
    account_type: str = "MASTER"
    parent_user_id: str | None = None
    parent_display_name: str | None = None


def _registration_budget(request: Request) -> None:
    from app.security_rate_limit import (
        DIMENSION_LOGIN_IP,
        client_ip_from_request,
        consume_rate_limit,
        login_ip_limit,
        rate_limit_window_seconds,
    )

    with pg_transaction() as conn:
        decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_IP,
            identifier="register:" + client_ip_from_request(request),
            limit=login_ip_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    if not decision.allowed:
        raise HTTPException(
            429,
            detail={"code": "RATE_LIMITED", "message": "注册尝试过于频繁，请稍后再试。"},
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _login_budget(request: Request, username: str) -> None:
    from app.customer_device_service import highest_device_domain_key, keyed_digest
    from app.security_rate_limit import (
        DIMENSION_LOGIN_ACCOUNT,
        DIMENSION_LOGIN_IP,
        client_ip_from_request,
        consume_rate_limit,
        login_account_limit,
        login_ip_limit,
        rate_limit_window_seconds,
    )

    _, key = highest_device_domain_key()
    decisions = []
    # Commit budgets even when the subsequent password/transaction fails.
    with pg_transaction() as conn:
        for dimension, identifier, limit in (
            (DIMENSION_LOGIN_IP, "password:" + client_ip_from_request(request), login_ip_limit()),
            (
                DIMENSION_LOGIN_ACCOUNT,
                keyed_digest(key, "password:" + username),
                login_account_limit(),
            ),
        ):
            decisions.append(
                consume_rate_limit(
                    conn,
                    dimension=dimension,
                    identifier=identifier,
                    limit=limit,
                    window_seconds=rate_limit_window_seconds(),
                )
            )
    denied = [item for item in decisions if not item.allowed]
    if denied:
        raise HTTPException(
            429,
            detail={
                "code": "RATE_LIMITED",
                "message": "登录尝试过于频繁，请稍后再试。",
            },
            headers={"Retry-After": str(max(item.retry_after_seconds for item in denied))},
        )


def _password_device(
    conn: psycopg.Connection,
    *,
    user_id: str,
    body: CustomerPasswordLoginRequest,
    parent_user_id: str | None = None,
) -> tuple[str, str]:
    """Resolve or mint this account's password-lane device row.

    A sub-account's device is bound to its master org (``parent_user_id``,
    the 20260919T1500 cascade column) so device history survives the sub
    account; masters keep the historical NULL.
    """
    from app.customer_device_service import (
        fingerprint_digests_for,
        highest_device_domain_key,
        keyed_digest,
    )

    version, key = highest_device_domain_key()
    fingerprints, fingerprint_version = fingerprint_digests_for(
        f"password-device:{user_id}:{body.device_fingerprint}",
    )
    row = conn.execute(
        "SELECT id FROM customer_devices WHERE user_id = %s AND fingerprint_hmac = ANY(%s) "
        "AND status = 'BOUND' AND activation_code_id IS NULL FOR UPDATE",
        (user_id, fingerprints),
    ).fetchone()
    device_token = secrets.token_urlsafe(32)
    token_digest = keyed_digest(key, device_token)
    if row is not None:
        device_id = str(row[0])
        # Re-login on a known device: refresh the credential and (re)assert
        # the org binding so a device created before the cascade migration
        # joins its sub-account's master org (a no-op for masters: NULL).
        conn.execute(
            "UPDATE customer_devices SET token_digest = %s, token_key_version = %s, "
            "parent_user_id = %s WHERE id = %s",
            (token_digest, version, parent_user_id, device_id),
        )
    else:
        device_id = str(uuid.uuid4())
        slots = conn.execute(
            "SELECT slot_no FROM customer_devices WHERE user_id = %s AND status = 'BOUND'",
            (user_id,),
        ).fetchall()
        used = {int(item[0]) for item in slots}
        slot = next(n for n in range(1, len(used) + 2) if n not in used)
        conn.execute(
            "INSERT INTO customer_devices "
            "(id, user_id, activation_code_id, parent_user_id, slot_no, display_name, "
            "platform, fingerprint_hmac, fingerprint_key_version, token_digest, "
            "token_key_version, fingerprint_canonical) "
            "VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                device_id,
                user_id,
                parent_user_id,
                slot,
                "账号登录设备",
                body.device_platform,
                fingerprints[-1],
                fingerprint_version,
                token_digest,
                version,
                fingerprints[0],
            ),
        )
    return device_id, device_token


@router.post("/login", response_model=CustomerPasswordLoginResponse)
def password_login(
    body: CustomerPasswordLoginRequest,
    request: Request,
    response: Response,
) -> CustomerPasswordLoginResponse:
    """Password -> real fenced session; never manufactures an activation code."""
    from app.activation_code_service import ActivationKeyError
    from app.customer_auth import SessionFencingError, verify_session_context
    from app.customer_device_service import (
        fingerprint_digests_for,
        highest_device_domain_key,
        keyed_digest,
    )
    from app.customer_idempotency import (
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
    from app.customer_session_service import login_session
    from app.ops_metrics import get_or_create_request_id
    from app.security_rate_limit import DIMENSION_LOGIN_ACCOUNT, record_auth_failure

    username = body.username.strip()
    key_text = request.headers.get("Idempotency-Key", "").strip()
    if not key_text or len(key_text) > 200:
        raise _http(400, "IDEMPOTENCY_KEY_REQUIRED", "请重新提交登录。")
    response.headers["Cache-Control"] = "no-store"
    try:
        get_pg_pool()
        _, device_key = highest_device_domain_key()
        aead_version, aead_key = highest_customer_aead_key()
        key_digests = idempotency_key_digests(key_text)
    except (RuntimeError, ValueError, ActivationKeyError, IdempotencyKeyError) as exc:
        raise _http(503, "SESSION_SERVICE_UNAVAILABLE", "登录服务暂不可用，请稍后重试。") from exc

    operation = "password_login"
    digest = request_hash(
        {
            "username": username,
            "password_proof": fingerprint_digests_for("password-login:" + body.password)[0][0],
            "device": body.device_fingerprint,
            "platform": body.device_platform,
            "takeover": str(body.takeover),
        }
    )
    replay_candidate = False
    account_usable = False
    with pg_transaction() as conn:
        row = conn.execute(
            "SELECT id, username, display_name, password_hash, is_active, role, "
            "registration_source, account_type, parent_user_id "
            "FROM users WHERE username = %s",
            (username,),
        ).fetchone()
        if row is not None:
            # Admission covers the sub-account lane: an admin-created SUB with
            # a bound, still-active master may hold a password session. The
            # master liveness read rides the lookup transaction and is
            # re-checked under the FOR UPDATE below before a session is minted.
            account_usable = (
                bool(row[4])
                and row[5] == "customer"
                and password_login_account_ok(
                    conn,
                    registration_source=row[6],
                    account_type=row[7],
                    parent_user_id=str(row[8]) if row[8] is not None else None,
                )
            )
            for key_digest in key_digests:
                envelope = load_envelope(
                    conn, operation=operation, scope=str(row[0]), key_digest=key_digest
                )
                if envelope is not None and envelope.request_hash == digest:
                    replay_candidate = True
                    break
    if not replay_candidate:
        _login_budget(request, username)
    encoded = str(row[3]) if row is not None and row[3] else _DUMMY_PASSWORD_HASH
    password_matches = verify_password(body.password, encoded)
    if row is None or not account_usable or not password_matches:
        with pg_transaction() as conn:
            record_auth_failure(
                conn,
                dimension=DIMENSION_LOGIN_ACCOUNT,
                identifier=keyed_digest(device_key, "password:" + username),
                request_id=get_or_create_request_id(request),
            )
        raise _http(401, "INVALID_CREDENTIALS", "用户名或密码错误，或账号暂不可用。")

    user_id = str(row[0])
    with pg_transaction() as conn:
        # Serializes first-device creation and rechecks account revocation/password changes.
        current = conn.execute(
            "SELECT password_hash, is_active, role, registration_source, "
            "account_type, parent_user_id FROM users "
            "WHERE id = %s FOR UPDATE",
            (user_id,),
        ).fetchone()
        if (
            current is None
            or current[0] != encoded
            or not current[1]
            or current[2] != "customer"
            or not password_login_account_ok(
                conn,
                registration_source=current[3],
                account_type=current[4],
                parent_user_id=str(current[5]) if current[5] is not None else None,
            )
        ):
            raise _http(401, "INVALID_CREDENTIALS", "用户名或密码错误，或账号暂不可用。")
        now_row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert now_row is not None
        now = now_row[0]
        for key_digest in key_digests:
            envelope = load_envelope(
                conn, operation=operation, scope=user_id, key_digest=key_digest
            )
            if envelope is None:
                continue
            if envelope.request_hash != digest:
                raise _http(409, "IDEMPOTENCY_CONFLICT", "登录信息已变化，请重新提交。")
            if (
                not envelope.ciphertext
                or not envelope.key_version
                or not envelope.recovery_expires_at
            ):
                raise _http(409, "LOGIN_RETRY_EXPIRED", "请重新提交登录。")
            from datetime import datetime

            if now >= datetime.fromisoformat(str(envelope.recovery_expires_at)):
                raise _http(409, "LOGIN_RETRY_EXPIRED", "请重新提交登录。")
            try:
                restored = CustomerPasswordLoginResponse.model_validate(
                    open_response(
                        envelope.ciphertext,
                        key=customer_aead_key(envelope.key_version),
                        aad=envelope_aad(operation, user_id, key_digest),
                    )
                )
            except (IdempotencyKeyError, ValueError) as exc:
                raise _http(
                    503, "SESSION_SERVICE_UNAVAILABLE", "登录恢复服务暂不可用，请稍后重试。"
                ) from exc
            try:
                verify_session_context(conn, presentation_session_token=restored.session_token)
            except SessionFencingError as exc:
                raise _http(401, exc.code, "登录已失效，请重新登录。") from exc
            response.headers["X-Idempotent-Replay"] = "true"
            return restored

        assert current is not None  # the guard above rejects a missing row
        parent_user_id = str(current[5]) if current[5] is not None else None
        parent_display_name: str | None = None
        if parent_user_id is not None:
            parent_row = conn.execute(
                "SELECT display_name FROM users WHERE id = %s", (parent_user_id,)
            ).fetchone()
            parent_display_name = str(parent_row[0]) if parent_row is not None else None
        device_id, device_token = _password_device(
            conn, user_id=user_id, body=body, parent_user_id=parent_user_id
        )
        result = login_session(
            conn,
            user_id=user_id,
            activation_code_id=None,
            device_id=device_id,
            presentation_session_token=None,
            request_id=get_or_create_request_id(request),
            now=now,
            takeover=body.takeover,
        )
        answer = CustomerPasswordLoginResponse(
            user_id=user_id,
            username=str(row[1]),
            display_name=str(row[2]),
            device_id=device_id,
            device_token=device_token,
            session_id=result.session_id,
            session_token=result.session_token or "",
            session_epoch=result.session_epoch,
            session_lease_expires_at=result.lease_until,
            account_type=str(current[4]),
            parent_user_id=parent_user_id,
            parent_display_name=parent_display_name,
        )
        envelope_id = insert_envelope(
            conn,
            operation=operation,
            scope=user_id,
            key_digest=key_digests[0],
            request_hash=digest,
        )
        if envelope_id is None:
            raise _http(409, "IDEMPOTENCY_CONFLICT", "请重新提交登录。")
        complete_envelope(
            conn,
            envelope_id,
            ciphertext=seal_response(
                answer.model_dump(),
                key=aead_key,
                aad=envelope_aad(operation, user_id, key_digests[0]),
            ),
            key_version=aead_version,
            recovery_expires_at=(now + timedelta(seconds=recovery_window_seconds())).isoformat(),
        )
        return answer


# Browser transport: only HttpOnly cookies contain bearer credentials. The
# strings returned to JS are CSRF handles, unusable without the matching cookie.
# Existing route verifiers, transaction fencing and idempotency stay authoritative.
class CustomerBrowserTransport:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if request.headers.get("X-Customer-Web") != "1":
            await self.app(scope, receive, send)
            return
        try:
            _require_browser_origin(request)
            await self._browser_request(request, scope, receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code)(
                scope, receive, send
            )

    async def _browser_request(
        self, request: Request, scope: Scope, receive: Receive, send: Send
    ) -> None:
        path = request.url.path
        cookies = request.cookies
        authorization = request.headers.get("Authorization", "")
        handle = authorization.removeprefix("Bearer ")
        kind = next((k for k in ("device", "session") if handle.startswith(f"web-{k}:")), None)
        raw_token = ""
        if kind:
            raw_token = cookies.get(_BROWSER_COOKIES[kind], "")
            if (
                not raw_token
                or not handle.isascii()
                or not hmac.compare_digest(handle, _browser_handle(kind, raw_token))
            ):
                raise _http(403, "BROWSER_CSRF_INVALID", "登录状态已变化，请刷新页面后重试。")
        elif authorization and path not in _BROWSER_SESSION_PATHS:
            raise _http(403, "BROWSER_CREDENTIAL_REQUIRED", "浏览器请求需要 Cookie 会话。")

        if path == "/api/customer/browser-session":
            response: Response
            if request.method == "GET":
                response = JSONResponse(
                    {
                        f"{k}_token": _browser_handle(k, cookies[_BROWSER_COOKIES[k]])
                        if cookies.get(_BROWSER_COOKIES[k])
                        else None
                        for k in ("device", "session")
                    }
                )
            elif request.method == "DELETE" and kind:
                response = Response(status_code=204)
                response.delete_cookie(_BROWSER_COOKIES["session"], path="/api")
                if kind == "device":
                    response.delete_cookie(_BROWSER_COOKIES["device"], path="/api")
            else:
                raise _http(403, "BROWSER_CSRF_REQUIRED", "请刷新登录状态后重试。")
            response.headers["Cache-Control"] = "no-store"
            await response(scope, receive, send)
            return

        if kind:
            scope = {
                **scope,
                "headers": [
                    (key, value)
                    for key, value in scope["headers"]
                    if key.lower() != b"authorization"
                ]
                + [(b"authorization", f"Bearer {raw_token}".encode())],
            }
        if path in _BROWSER_SESSION_PATHS and request.method == "POST":
            raw = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                raw.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                body = None
            if (
                isinstance(body, dict)
                and isinstance(body.get("session_token"), str)
                and (kind or body["session_token"].startswith("web-session:"))
            ):
                session_cookie = cookies.get(_BROWSER_COOKIES["session"], "")
                if (
                    not session_cookie
                    or not body["session_token"].isascii()
                    or not hmac.compare_digest(
                        body["session_token"], _browser_handle("session", session_cookie)
                    )
                ):
                    raise _http(403, "BROWSER_CSRF_INVALID", "登录状态已变化，请刷新页面后重试。")
                body["session_token"] = session_cookie
                raw = bytearray(json.dumps(body).encode())
            scope = {
                **scope,
                "headers": [
                    (key, value)
                    for key, value in scope["headers"]
                    if key.lower() != b"content-length"
                ]
                + [(b"content-length", str(len(raw)).encode())],
            }
            consumed = False
            original_receive = receive

            async def rewritten_receive() -> Message:
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": bytes(raw), "more_body": False}
                return await original_receive()

            receive = rewritten_receive

        if path not in _BROWSER_LOGIN_PATHS or request.method != "POST":
            await self.app(scope, receive, send)
            return
        start: Message | None = None
        chunks = bytearray()

        async def cookie_response(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                if not 200 <= int(message["status"]) < 300:
                    await send(message)
            elif message["type"] == "http.response.body" and start is not None:
                if not 200 <= int(start["status"]) < 300:
                    await send(message)
                    return
                chunks.extend(message.get("body", b""))
                if message.get("more_body", False):
                    return
                payload = json.loads(chunks)
                # Upgrade an already authenticated browser's in-memory device
                # credential through the normal login verifier. No new session
                # authority is invented; the device/lease checks ran above.
                if (
                    path in _BROWSER_SESSION_PATHS
                    and not kind
                    and authorization.startswith("Bearer ")
                ):
                    payload["device_token"] = handle
                response = Response(status_code=start["status"], media_type="application/json")
                response.raw_headers = [
                    (key, value)
                    for key, value in start["headers"]
                    if key.lower() not in (b"content-length", b"cache-control")
                ]
                for token_kind in ("device", "session"):
                    field = f"{token_kind}_token"
                    token = payload.get(field)
                    if isinstance(token, str) and token:
                        response.set_cookie(
                            _BROWSER_COOKIES[token_kind],
                            token,
                            path="/api",
                            httponly=True,
                            secure=is_customer_production(),
                            samesite="strict",
                        )
                        payload[field] = _browser_handle(token_kind, token)
                response.body = json.dumps(payload, ensure_ascii=False).encode()
                response.headers["Content-Length"] = str(len(response.body))
                response.headers["Cache-Control"] = "no-store"
                await response(scope, receive, send)
            else:
                await send(message)

        await self.app(scope, receive, cookie_response)


_BROWSER_COOKIES = {"device": "customer_web_device", "session": "customer_web_session"}
_BROWSER_SESSION_PATHS = {"/api/customer/sessions/login", "/api/customer/sessions/switch"}
_BROWSER_LOGIN_PATHS = _BROWSER_SESSION_PATHS | {"/api/customer/login", "/api/customer/activate"}


def _browser_handle(kind: str, token: str) -> str:
    digest = hmac.new(token.encode(), b"customer-browser-csrf-v1", hashlib.sha256).hexdigest()
    return f"web-{kind}:{digest}"


def _require_browser_origin(request: Request) -> None:
    # Custom header is deliberately absent from the cross-origin CORS allowlist.
    # SameSite and Fetch Metadata are additional protections, not replacements
    # for the per-cookie CSRF proof required by authenticated requests above.
    origin = request.headers.get("origin")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise _http(403, "BROWSER_ORIGIN_INVALID", "浏览器请求来源不匹配。")
    if origin:
        parsed = urlsplit(origin)
        allowed = (
            origin == customer_public_origin()
            if is_customer_production()
            else (
                parsed.scheme in {"http", "https"}
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            )
        )
        if not allowed:
            raise _http(403, "BROWSER_ORIGIN_INVALID", "浏览器请求来源不匹配。")
