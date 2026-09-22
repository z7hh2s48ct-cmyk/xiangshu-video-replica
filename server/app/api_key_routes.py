"""CW-078 — the customer API-Key management routes (§2.5 B / §5 line 336).

``/api/customer/api-keys`` lets a customer mint, list and revoke the API Keys
their programs present on the independent lane. Management itself rides the
**customer session fence** — never an API Key (§2.2: the whitelist explicitly
excludes 密钥管理自身, so a leaked key cannot mint or revoke its own kind).

Red lines (R-A / §2.5 B):

- The plaintext ``xsk_live_...`` is returned on creation/rotation, with short
  encrypted idempotent retry recovery. The list/detail responses carry only
  metadata — ``key_prefix`` and the stored ``scopes`` — and never the
  ``key_digest`` or the plaintext.
- Revoke is a soft revoke scoped to the caller's own keys: a missing key or one
  belonging to another user is the single 404 ``API_KEY_NOT_FOUND`` (no IDOR
  oracle); repeating a revoke is an idempotent 204.
- Lifecycle events land in ``audit_logs`` with public metadata only. Key digests
  remain in ``customer_api_keys``; retry envelopes hold encrypted responses.
  Plaintext secrets are never persisted or audited.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict

from app.api_key_service import (
    REVOKE_OUTCOME_NOT_FOUND,
    REVOKE_OUTCOME_REVOKED,
    ApiKeyError,
    ApiKeyRecord,
    create_api_key,
    list_api_keys,
    revoke_api_key,
)
from app.customer_fence import (
    CustomerSessionSnapshot,
    customer_session_snapshot,
    fenced_pg_transaction,
)
from app.sub_account_permissions import enforce_sub_account_api_keys

router = APIRouter(prefix="/api/customer/api-keys", tags=["customer-api-keys"])

# A label is a human reminder only; bound it so a careless paste cannot store a
# megabyte per key. The plaintext/digest are never user-supplied.
API_KEY_LABEL_MAX = 100


class CreateApiKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ""


class ApiKeyRecordResponse(BaseModel):
    """Public metadata for one logical Token; never its digest."""

    model_config = ConfigDict(extra="forbid")
    id: str
    key_prefix: str
    label: str
    scopes: list[str]
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    token_group_id: str = ""
    credential_version: int = 1
    is_default: bool = False
    total_consumed_credits: int = 0


class CreatedApiKeyResponse(ApiKeyRecordResponse):
    """Secret only for newly created credentials or short encrypted retry recovery."""

    plaintext: str | None


class ApiKeyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ApiKeyRecordResponse]
    total: int


def _require_customer_snapshot(request: Request) -> CustomerSessionSnapshot:
    """Management is session-fenced: no session token, no key management."""
    snapshot = customer_session_snapshot(request)
    if snapshot is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "SESSION_REQUIRED",
                "message": "A customer session token is required.",
            },
        )
    return snapshot


def _insert_customer_audit(
    conn: psycopg.Connection,
    *,
    user_id: str,
    action: str,
    entity_id: str,
    metadata: dict[str, object] | None = None,
) -> None:
    """Append one key-lifecycle audit row (prefix/scopes only, never secrets)."""
    conn.execute(
        "INSERT INTO audit_logs "
        "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            str(uuid4()),
            user_id,
            action,
            "api_key",
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def _record_response(record: ApiKeyRecord) -> ApiKeyRecordResponse:
    return ApiKeyRecordResponse.model_validate(asdict(record))


def _lock_customer(conn: psycopg.Connection, user_id: str) -> None:
    row = conn.execute(
        "SELECT is_active, role FROM users WHERE id = %s FOR UPDATE", (user_id,)
    ).fetchone()
    if row is None or not row[0] or row[1] != "customer":
        raise HTTPException(
            401, detail={"code": "ACCOUNT_UNAVAILABLE", "message": "账号已停用，请联系管理员。"}
        )


def _mutate_key(
    request: Request, response: Response, *, operation: str, label: str = "", key_id: str = ""
) -> CreatedApiKeyResponse:
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

    snapshot = _require_customer_snapshot(request)
    retry_key = request.headers.get("Idempotency-Key", "").strip()
    if not retry_key or len(retry_key) > 200:
        raise HTTPException(
            400, detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "请重新提交 Token 操作。"}
        )
    response.headers["Cache-Control"] = "no-store"
    try:
        aead_version, aead_key = highest_customer_aead_key()
        digests = idempotency_key_digests(retry_key)
        maximum = int(os.environ.get("VIDEO_REPLICA_MAX_CUSTOMER_API_KEYS", "50"))
        if not 1 <= maximum <= 1000:
            raise ValueError("invalid Token limit")
    except (IdempotencyKeyError, ValueError) as exc:
        raise HTTPException(
            503, detail={"code": "TOKEN_SERVICE_UNAVAILABLE", "message": "Token 服务配置暂不可用。"}
        ) from exc
    op = "api_key:" + operation
    fingerprint = request_hash({"label": label, "key_id": key_id})
    try:
        with fenced_pg_transaction(snapshot) as (conn, ctx):
            _lock_customer(conn, ctx.user_id)
            # Token admission (Phase 3b): create/default/rotate are one gate,
            # replays included, so re-enabling the switch is the only way back.
            enforce_sub_account_api_keys(conn, actor_id=ctx.user_id)
            now_row = conn.execute("SELECT clock_timestamp()").fetchone()
            assert now_row is not None
            now = now_row[0]
            for digest in digests:
                envelope = load_envelope(conn, operation=op, scope=ctx.user_id, key_digest=digest)
                if envelope is None:
                    continue
                if envelope.request_hash != fingerprint:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "IDEMPOTENCY_CONFLICT",
                            "message": "本次操作内容已变化，请重新提交。",
                        },
                    )
                if (
                    not envelope.ciphertext
                    or not envelope.key_version
                    or not envelope.recovery_expires_at
                    or now >= datetime.fromisoformat(str(envelope.recovery_expires_at))
                ):
                    raise HTTPException(
                        409,
                        detail={
                            "code": "TOKEN_RETRY_EXPIRED",
                            "message": "恢复窗口已结束，请刷新列表后重试。",
                        },
                    )
                answer = CreatedApiKeyResponse.model_validate(
                    open_response(
                        envelope.ciphertext,
                        key=customer_aead_key(envelope.key_version),
                        aad=envelope_aad(op, ctx.user_id, digest),
                    )
                )
                if answer.plaintext is not None:
                    current = conn.execute(
                        "SELECT 1 FROM customer_api_keys "
                        "WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                        (answer.id, ctx.user_id),
                    ).fetchone()
                    if current is None:
                        raise HTTPException(
                            409,
                            detail={
                                "code": "TOKEN_CREDENTIAL_CHANGED",
                                "message": "这枚凭据已更新或撤销，请刷新列表。",
                            },
                        )
                response.headers["X-Idempotent-Replay"] = "true"
                return answer
            records = list_api_keys(conn, user_id=ctx.user_id)
            existing = (
                next((r for r in records if r.is_default), None) if operation == "default" else None
            )
            plaintext = None
            if existing is not None:
                record = existing
            elif operation == "rotate":
                old = next((r for r in records if r.id == key_id), None)
                if old is None:
                    raise HTTPException(
                        404, detail={"code": "API_KEY_NOT_FOUND", "message": "Token 不存在。"}
                    )
                if old.revoked_at is not None:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "TOKEN_REVOKED",
                            "message": "已撤销的 Token 无法更新，请新建 Token。",
                        },
                    )
                revoke_api_key(conn, user_id=ctx.user_id, key_id=old.id)
                plaintext, record = create_api_key(
                    conn,
                    user_id=ctx.user_id,
                    label=old.label,
                    scopes=old.scopes,
                    token_group_id=old.token_group_id,
                    credential_version=old.credential_version + 1,
                    is_default=old.is_default,
                )
            else:
                if sum(r.revoked_at is None for r in records) >= maximum:
                    raise HTTPException(
                        409,
                        detail={
                            "code": "TOKEN_LIMIT_REACHED",
                            "message": "有效 Token 数量已达上限，请先撤销不再使用的 Token。",
                        },
                    )
                plaintext, record = create_api_key(
                    conn,
                    user_id=ctx.user_id,
                    label="默认 Token" if operation == "default" else (label or "未命名 Token"),
                    is_default=operation == "default",
                )
            if plaintext is not None:
                _insert_customer_audit(
                    conn,
                    user_id=ctx.user_id,
                    action="customer.api_key."
                    + ("rotated" if operation == "rotate" else "created"),
                    entity_id=record.id,
                    metadata={
                        "key_prefix": record.key_prefix,
                        "scopes": list(record.scopes),
                        "token_group_id": record.token_group_id,
                        "previous_key_id": key_id or None,
                    },
                )
            answer = CreatedApiKeyResponse(
                **_record_response(record).model_dump(), plaintext=plaintext
            )
            envelope_id = insert_envelope(
                conn,
                operation=op,
                scope=ctx.user_id,
                key_digest=digests[0],
                request_hash=fingerprint,
            )
            if envelope_id is None:
                raise HTTPException(
                    409, detail={"code": "IDEMPOTENCY_CONFLICT", "message": "请刷新列表后重试。"}
                )
            complete_envelope(
                conn,
                envelope_id,
                ciphertext=seal_response(
                    answer.model_dump(), key=aead_key, aad=envelope_aad(op, ctx.user_id, digests[0])
                ),
                key_version=aead_version,
                recovery_expires_at=(
                    now + timedelta(seconds=recovery_window_seconds())
                ).isoformat(),
            )
            return answer
    except (ApiKeyError, IdempotencyKeyError, ValueError) as exc:
        raise HTTPException(
            503,
            detail={
                "code": "TOKEN_SERVICE_UNAVAILABLE",
                "message": "Token 服务暂不可用，请稍后重试。",
            },
        ) from exc


@router.post("", response_model=CreatedApiKeyResponse, status_code=201)
def create_customer_api_key(
    payload: CreateApiKeyRequest, request: Request, response: Response
) -> CreatedApiKeyResponse:
    label = payload.label.strip()
    if len(label) > API_KEY_LABEL_MAX or any(ord(c) < 32 for c in label):
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_API_KEY_LABEL",
                "message": "Token 名称最多 100 个字符，不能包含控制字符。",
            },
        )
    return _mutate_key(request, response, operation="create", label=label)


@router.post("/default", response_model=CreatedApiKeyResponse, status_code=201)
def initialize_default_api_key(request: Request, response: Response) -> CreatedApiKeyResponse:
    return _mutate_key(request, response, operation="default")


@router.post("/{key_id}/rotate", response_model=CreatedApiKeyResponse, status_code=201)
def rotate_customer_api_key(
    key_id: str, request: Request, response: Response
) -> CreatedApiKeyResponse:
    return _mutate_key(request, response, operation="rotate", key_id=key_id)


@router.get("", response_model=ApiKeyListResponse)
def list_customer_api_keys(request: Request, response: Response) -> ApiKeyListResponse:
    """List this customer's keys (including revoked, as audit) — metadata only."""
    response.headers["Cache-Control"] = "no-store"
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        records = list_api_keys(conn, user_id=ctx.user_id)
    return ApiKeyListResponse(
        items=[_record_response(record) for record in records],
        total=len(records),
    )


@router.delete(
    "/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_customer_api_key(key_id: str, request: Request) -> Response:
    """Soft-revoke one of this customer's keys; idempotent, no IDOR oracle."""
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_customer(conn, ctx.user_id)
        outcome = revoke_api_key(conn, user_id=ctx.user_id, key_id=key_id)
        if outcome == REVOKE_OUTCOME_NOT_FOUND:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "API_KEY_NOT_FOUND",
                    "message": "API key does not exist.",
                },
            )
        if outcome == REVOKE_OUTCOME_REVOKED:
            _insert_customer_audit(
                conn,
                user_id=ctx.user_id,
                action="customer.api_key.revoked",
                entity_id=key_id,
            )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})
