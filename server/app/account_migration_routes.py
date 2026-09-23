"""Initial password setup for an authenticated legacy activation account."""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from app.activation_code_service import ActivationKeyError
from app.api_key_routes import _lock_customer, _require_customer_snapshot
from app.customer_auth_routes import (
    CustomerRegistrationRequest,
    _normalize_username,
    _registration_budget,
)
from app.customer_device_service import fingerprint_digests_for
from app.customer_fence import fenced_pg_transaction
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
from app.password_hashing import PasswordPolicyError, hash_password

router = APIRouter(prefix="/api/customer/account", tags=["account-migration"])


class PasswordState(BaseModel):
    user_id: str
    username: str
    has_password: bool


@router.get("/password", response_model=PasswordState)
def password_state(request: Request, response: Response) -> PasswordState:
    response.headers["Cache-Control"] = "no-store"
    with fenced_pg_transaction(_require_customer_snapshot(request)) as (conn, ctx):
        _lock_customer(conn, ctx.user_id)
        row = conn.execute(
            "SELECT username, password_hash IS NOT NULL FROM users WHERE id = %s", (ctx.user_id,)
        ).fetchone()
        assert row is not None
        return PasswordState(user_id=ctx.user_id, username=row[0], has_password=row[1])


@router.post("/password", response_model=PasswordState)
def set_initial_password(
    body: CustomerRegistrationRequest, request: Request, response: Response
) -> PasswordState:
    snapshot = _require_customer_snapshot(request)
    username = _normalize_username(body.username)
    retry = request.headers.get("Idempotency-Key", "").strip()
    if not retry or len(retry) > 200:
        raise HTTPException(400, detail="请重新提交账号设置。")
    response.headers["Cache-Control"] = "no-store"
    try:
        version, key = highest_customer_aead_key()
        digests = idempotency_key_digests(retry)
        fingerprint = request_hash(
            {
                "username": username,
                "password_proof": fingerprint_digests_for("legacy-password:" + body.password)[0][0],
            }
        )
        _registration_budget(request)
        encoded = hash_password(body.password)
    except PasswordPolicyError as exc:
        raise HTTPException(400, detail={"code": "WEAK_PASSWORD", "message": str(exc)}) from exc
    except (IdempotencyKeyError, ActivationKeyError) as exc:
        raise HTTPException(503, detail="账号设置暂不可用。") from exc
    operation = "account:initial_password"
    try:
        with fenced_pg_transaction(snapshot) as (conn, ctx):
            _lock_customer(conn, ctx.user_id)
            clock_row = conn.execute("SELECT clock_timestamp()").fetchone()
            assert clock_row is not None
            now = clock_row[0]
            for digest in digests:
                envelope = load_envelope(
                    conn, operation=operation, scope=ctx.user_id, key_digest=digest
                )
                if envelope is None:
                    continue
                if envelope.request_hash != fingerprint:
                    raise HTTPException(409, detail="本次账号设置内容已变化，请重新提交。")
                if (
                    not envelope.ciphertext
                    or not envelope.key_version
                    or not envelope.recovery_expires_at
                    or now >= datetime.fromisoformat(envelope.recovery_expires_at)
                ):
                    raise HTTPException(409, detail="恢复窗口已结束，请重新读取账号设置。")
                response.headers["X-Idempotent-Replay"] = "true"
                return PasswordState.model_validate(
                    open_response(
                        envelope.ciphertext,
                        key=customer_aead_key(envelope.key_version),
                        aad=envelope_aad(operation, ctx.user_id, digest),
                    )
                )
            row = conn.execute(
                "SELECT password_hash, registration_source FROM users WHERE id = %s", (ctx.user_id,)
            ).fetchone()
            if row is None or row[0] is not None or ctx.activation_code_id is None:
                raise HTTPException(409, detail="此账号已设置密码或不属于旧激活账号。")
            conn.execute(
                "UPDATE users SET username = %s, password_hash = %s, "
                "registration_source = 'activation_code' WHERE id = %s",
                (username, encoded, ctx.user_id),
            )
            conn.execute(
                "INSERT INTO audit_logs (id, actor_user_id, action, "
                "entity_type, entity_id, metadata_json) VALUES (%s, %s, "
                "'customer.password.initialized', 'user', %s, %s)",
                (
                    str(uuid4()),
                    ctx.user_id,
                    ctx.user_id,
                    json.dumps({"previous_registration_source": row[1]}),
                ),
            )
            answer = PasswordState(user_id=ctx.user_id, username=username, has_password=True)
            envelope_id = insert_envelope(
                conn,
                operation=operation,
                scope=ctx.user_id,
                key_digest=digests[0],
                request_hash=fingerprint,
            )
            if envelope_id is None:
                raise HTTPException(409, detail="账号设置正在处理，请重试。")
            complete_envelope(
                conn,
                envelope_id,
                ciphertext=seal_response(
                    answer.model_dump(),
                    key=key,
                    aad=envelope_aad(operation, ctx.user_id, digests[0]),
                ),
                key_version=version,
                recovery_expires_at=(
                    now + timedelta(seconds=recovery_window_seconds())
                ).isoformat(),
            )
            return answer
    except UniqueViolation as exc:
        if exc.diag.constraint_name == "users_username_key":
            raise HTTPException(409, detail="该用户名已被使用，请选择其他名称。") from exc
        raise
    except (IdempotencyKeyError, ActivationKeyError) as exc:
        raise HTTPException(503, detail="账号设置暂不可用。") from exc
