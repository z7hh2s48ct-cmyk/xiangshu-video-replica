"""C5 publish module (phase 1): platform accounts and account-verify claims.

The studio publish page previously kept drafts in React memory with every
real action disabled. This module is the first half of the backend contract
that unlocks it — account authorization only:

- ``publish_accounts`` hold platform creator cookies (douyin additionally the
  security_sdk risk-control material) encrypted at rest with the settings
  Fernet key; responses never contain credential material;
- account verification is an async worker probe that flips the
  connected/invalid status without ever exposing the cookie, claimed with a
  CAS lease (``FOR UPDATE SKIP LOCKED`` plus a fenced write-back).

The formal delivery path lives in ``app.publish_records`` (PUBLISH-DELIVERY-
20260917): the ``publish_records`` queue, the worker publish claim and
``published_total`` are deliberately kept out of this module so the verify half
keeps working on an accounts-only schema (test A12). This cookie-paste account
table is legacy — deliveries read the browser-login accounts instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor

PLATFORMS: tuple[str, ...] = ("douyin", "wechat_channels")
# 抖音创作者网页端发布除 Cookie 外还需要 security_sdk 风控材料；
# 视频号助手仅凭 Cookie 即可。
PLATFORMS_REQUIRING_SECURITY_SDK: frozenset[str] = frozenset({"douyin"})

_VERIFY_LEASE_SECONDS = 120

MAX_DISPLAY_NAME_LENGTH = 60
MAX_COOKIE_LENGTH = 20000
MAX_SECURITY_SDK_LENGTH = 100000

_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class PublishLeaseLostError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _now_text() -> str:
    return _now().strftime(_TIME_FORMAT)


# ---------------------------------------------------------------------------
# API models (accounts)
# ---------------------------------------------------------------------------


class PublishAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    cookie: str = Field(min_length=10, max_length=MAX_COOKIE_LENGTH)
    security_sdk: str | None = Field(default=None, max_length=MAX_SECURITY_SDK_LENGTH)


class PublishAccountResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    platform: str
    display_name: str
    status: str
    last_verified_at: str | None
    error_message: str | None
    security_sdk_required: bool
    created_at: str


class PublishAccountListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[PublishAccountResponse]


class PublishAccountDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool


class PublishVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submitted: bool


# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------


def _bad_request(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _not_found(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=404, detail={"code": code, "message": message})


def _require_owned_account(
    conn: BusinessConnection, *, actor_id: str, account_id: str
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM publish_accounts WHERE id = %s AND user_id = %s",
        (account_id, actor_id),
    ).fetchone()
    if row is None:
        raise _not_found("PUBLISH_ACCOUNT_NOT_FOUND", "发布账号不存在或无权访问。")
    return dict(row)


# ---------------------------------------------------------------------------
# Account domain
# ---------------------------------------------------------------------------


def _account_response(row: dict[str, Any]) -> PublishAccountResponse:
    platform = str(row["platform"])
    return PublishAccountResponse(
        id=str(row["id"]),
        platform=platform,
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        last_verified_at=_text_or_none(row["last_verified_at"]),
        error_message=_text_or_none(row["error_message"]),
        security_sdk_required=platform in PLATFORMS_REQUIRING_SECURITY_SDK,
        created_at=str(row["created_at"]),
    )


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def create_account(
    conn: BusinessConnection,
    *,
    actor: Any,
    fernet: Fernet,
    request: PublishAccountCreateRequest,
) -> PublishAccountResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="publish.account.create",
        entity_type="publish_account",
        entity_id="new",
    )

    # T2.6: 验证当前用户必须是 MASTER 类型（子账号不能创建发布账号）
    user_row = conn.execute(
        "SELECT account_type FROM users WHERE id = %s",
        (actor.id,),
    ).fetchone()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found")
    account_type = user_row["account_type"]
    if account_type != "MASTER":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PUBLISH_ACCOUNT_MASTER_ONLY",
                "message": "发布账号必须由母账号创建，子账号无权操作。",
            },
        )

    platform = request.platform.strip()
    if platform not in PLATFORMS:
        raise _bad_request("PUBLISH_PLATFORM_UNSUPPORTED", "不支持的发布平台。")
    if platform in PLATFORMS_REQUIRING_SECURITY_SDK and not (
        request.security_sdk and request.security_sdk.strip()
    ):
        raise _bad_request(
            "PUBLISH_SECURITY_SDK_REQUIRED", "该平台需要同时粘贴 security_sdk 材料。"
        )
    account_id = str(uuid4())
    conn.execute(
        """
        INSERT INTO publish_accounts (
            id, user_id, platform, display_name, cookie_enc, security_sdk_enc, status
        ) VALUES (%s, %s, %s, %s, %s, %s, 'connected')
        """,
        (
            account_id,
            actor.id,
            platform,
            request.display_name.strip(),
            fernet.encrypt(request.cookie.strip().encode("utf-8")).decode("ascii"),
            (
                fernet.encrypt(request.security_sdk.strip().encode("utf-8")).decode("ascii")
                if request.security_sdk and request.security_sdk.strip()
                else None
            ),
        ),
    )
    row = conn.execute("SELECT * FROM publish_accounts WHERE id = %s", (account_id,)).fetchone()
    if row is None:  # pragma: no cover - insert then select in one connection
        raise RuntimeError("publish account insert was lost")
    conn.commit()
    return _account_response(dict(row))


def list_accounts(conn: BusinessConnection, *, actor_id: str) -> PublishAccountListResponse:
    rows = conn.execute(
        """
        SELECT * FROM publish_accounts
        WHERE user_id = %s
        ORDER BY created_at, id
        """,
        (actor_id,),
    ).fetchall()
    return PublishAccountListResponse(accounts=[_account_response(dict(row)) for row in rows])


def delete_account(conn: BusinessConnection, *, actor_id: str, account_id: str) -> None:
    cursor = conn.execute(
        "DELETE FROM publish_accounts WHERE id = %s AND user_id = %s",
        (account_id, actor_id),
    )
    if cursor.rowcount != 1:
        raise _not_found("PUBLISH_ACCOUNT_NOT_FOUND", "发布账号不存在或无权访问。")
    conn.commit()


def request_account_verify(conn: BusinessConnection, *, actor_id: str, account_id: str) -> None:
    _require_owned_account(conn, actor_id=actor_id, account_id=account_id)
    conn.execute(
        """
        UPDATE publish_accounts SET verify_requested = 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s AND user_id = %s
        """,
        (account_id, actor_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Worker claims / finalizers (mirror the oral_worker CAS pattern)
# Phase 1 keeps the account-verify half only.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishLease:
    kind: Literal["account_verify"]
    record_id: str
    worker_id: str
    lease_token: str
    attempt_count: int
    row: dict[str, Any]


def _quarantine_expired_verifies(conn: BusinessConnection, now_text: str) -> None:
    """Reset account-verify leases whose probe died mid-flight.

    Scoped to ``publish_accounts`` on purpose: claiming a verify must never touch
    ``publish_records``. That separation is what lets ``claim_account_verify_work``
    run against an accounts-only schema; the publish round has its own quarantine
    in ``app.publish_records``.
    """
    conn.execute(
        """
        UPDATE publish_accounts
        SET verify_requested = 0, lease_owner = NULL, lease_expires_at = NULL,
            error_message = %s, updated_at = CURRENT_TIMESTAMP
        WHERE verify_requested = 1 AND lease_expires_at IS NOT NULL
          AND lease_expires_at <= %s
        """,
        ("登录态校验中断，请重新发起验证", now_text),
    )


def _claim(
    conn: BusinessConnection,
    *,
    kind: Literal["account_verify"],
    table: str,
    current_state_sql: str,
    current_state_params: tuple[object, ...],
    claimed_assignments: str,
    claimed_row_updates: dict[str, object],
    candidate_sql: str,
    candidate_params: tuple[object, ...],
    worker_id: str,
    lease_seconds: int,
    now: datetime,
) -> PublishLease | None:
    now_text = now.strftime(_TIME_FORMAT)
    expires_text = (now + timedelta(seconds=lease_seconds)).strftime(_TIME_FORMAT)
    with conn:
        row = conn.execute(
            f"{candidate_sql} FOR UPDATE SKIP LOCKED",  # noqa: S608 - fixed literal
            candidate_params,
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        lease_token = uuid4().hex
        claimed = conn.execute(
            f"""
            UPDATE {table}
            SET {claimed_assignments}, lease_owner = %s, lease_expires_at = %s,
                attempt_count = attempt_count + 1, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND ({current_state_sql})
              AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
            RETURNING attempt_count
            """,  # noqa: S608 - table/assignments are fixed internal literals
            (
                lease_token,
                expires_text,
                str(record["id"]),
                *current_state_params,
                now_text,
            ),
        ).fetchone()
        if claimed is None:
            return None
        record["lease_owner"] = lease_token
        record["attempt_count"] = int(claimed["attempt_count"])
        record.update(claimed_row_updates)
        return PublishLease(
            kind=kind,
            record_id=str(record["id"]),
            worker_id=worker_id,
            lease_token=lease_token,
            attempt_count=int(claimed["attempt_count"]),
            row=record,
        )


_VERIFY_CANDIDATE_SQL = """
    SELECT * FROM publish_accounts
    WHERE verify_requested = 1
      AND (lease_expires_at IS NULL OR lease_expires_at <= %s)
    ORDER BY created_at, id
    LIMIT 1
"""


def claim_account_verify_work(
    conn: BusinessConnection,
    *,
    worker_id: str,
    lease_seconds: int = _VERIFY_LEASE_SECONDS,
    now: datetime | None = None,
) -> PublishLease | None:
    moment = now or _now()
    now_text = moment.strftime(_TIME_FORMAT)
    with conn:
        _quarantine_expired_verifies(conn, now_text)
    return _claim(
        conn,
        kind="account_verify",
        table="publish_accounts",
        current_state_sql="verify_requested = 1",
        current_state_params=(),
        claimed_assignments="status = status",
        claimed_row_updates={},
        candidate_sql=_VERIFY_CANDIDATE_SQL,
        candidate_params=(now_text,),
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        now=moment,
    )


def finalize_account_verify(
    conn: BusinessConnection,
    *,
    lease: PublishLease,
    ok: bool,
    message: str | None = None,
) -> None:
    with conn:
        cursor = conn.execute(
            """
            UPDATE publish_accounts
            SET verify_requested = 0, status = %s, last_verified_at = %s,
                error_message = %s, lease_owner = NULL, lease_expires_at = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND verify_requested = 1 AND lease_owner = %s
              AND attempt_count = %s
            """,
            (
                "connected" if ok else "invalid",
                _now_text(),
                None if ok else (message or "登录态校验未通过"),
                lease.record_id,
                lease.lease_token,
                lease.attempt_count,
            ),
        )
        if cursor.rowcount != 1:
            raise PublishLeaseLostError("account verify lease was lost")
