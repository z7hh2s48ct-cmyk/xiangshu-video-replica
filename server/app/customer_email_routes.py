"""客户邮箱绑定与邮箱找回密码.

两条泳道：

1. **绑定邮箱**（会话围栏内）：``send-code`` 把 6 位验证码发到待绑定地址，
   ``verify`` 核对通过后才写进 ``users.email``。未验证的地址只活在验证码行里，
   因此找回密码永远只会发往一个已证明归属的邮箱。只有主账号能绑定——子账号的
   密码由主账号重置（``customer_sub_account_routes``），给它开邮箱找回等于绕开
   主账号的管理权。
2. **找回密码**（无会话）：``forgot`` 按用户名或邮箱定位账号并发码，``reset``
   凭验证码设新密码。用户名不允许含 ``@``（``customer_auth_routes``），所以
   输入里有 ``@`` 就按邮箱查，没有就按用户名查，不存在歧义。

防枚举：``forgot`` 对「账号不存在 / 没绑邮箱 / 子账号 / 冷却中」一律回同一个
202，且邮件在响应之后才由后台任务发出——同步调用发信接口的几百毫秒延迟本身
就会泄露「这个账号绑了邮箱」。``reset`` 对未知账号与错码回同一个 400。

验证码只存带密钥的摘要（设备域密钥，与指纹同域，轮换期间按全部版本比对），
15 分钟过期，最多试错 5 次，新码作废旧码。预算复用既有维度加前缀（与改密同
理由：新维度要改 ``security_auth_failures`` 的 CHECK）。重置成功即撤销在线会话
——丢了密码的人，往往正是因为别人拿着它。
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta, timezone
from functools import partial
from typing import Literal

import psycopg
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.admin_write_contract import transaction_now_iso
from app.api_errors import http_error as _http
from app.api_key_routes import (
    _insert_customer_audit,
    _lock_customer,
    _require_customer_snapshot,
)
from app.customer_device_service import (
    fingerprint_digests_for,
    highest_device_domain_key,
    keyed_digest,
)
from app.customer_fence import fenced_pg_transaction
from app.customer_security_routes import _client_context, _live_session_count, _request_id
from app.customer_session_service import REASON_PASSWORD_RESET, revoke_session
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.email_delivery import EmailSender, deliver_quietly, email_sender_from_settings
from app.password_hashing import PasswordPolicyError, hash_password
from app.security_rate_limit import (
    DIMENSION_LOGIN_ACCOUNT,
    DIMENSION_LOGIN_IP,
    client_ip_from_request,
    consume_rate_limit,
    login_account_limit,
    login_ip_limit,
    rate_limit_window_seconds,
)
from app.settings import SettingsUnavailableError
from app.sub_account_auth import password_login_account_ok

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/customer", tags=["customer-email"])

Purpose = Literal["bind_email", "reset_password"]
PURPOSE_BIND: Purpose = "bind_email"
PURPOSE_RESET: Purpose = "reset_password"
_ACTION_LABELS: dict[Purpose, str] = {PURPOSE_BIND: "绑定邮箱", PURPOSE_RESET: "重置密码"}

CODE_TTL_MINUTES = 15
MAX_CODE_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 60
MAX_EMAIL_LENGTH = 254

# 只挡明显不是邮箱的输入；地址真不真由验证码证明，正则再严也证明不了。
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BEIJING = timezone(timedelta(hours=8))

# 找回与重置共用 ``login:*`` 维度，前缀把计数器与登录、改密隔开。
_FORGOT_PREFIX = "password-forgot:"
_RESET_PREFIX = "password-reset:"
_BIND_PREFIX = "email-bind:"

FORGOT_ACCEPTED_MESSAGE = "如果该账号已绑定邮箱，验证码已发送，请查收邮件。"
_INVALID_CODE_MESSAGE = "验证码不正确或已失效，请检查后重试或重新获取。"
_SERVICE_UNAVAILABLE_MESSAGE = "邮件服务暂未开通，请联系客服。"


class EmailStateResponse(BaseModel):
    email: str | None
    verified_at: str | None
    # 子账号不能绑定（见模块 docstring），界面据此不展示引导。
    can_bind: bool
    # 发信未配置时不引导绑定：绑了也收不到码，只会制造一次失败体验。
    service_available: bool


class SendBindCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=MAX_EMAIL_LENGTH)


class CodeSentResponse(BaseModel):
    sent: bool
    expires_in_minutes: int
    resend_after_seconds: int


class VerifyBindCodeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: StrictStr = Field(min_length=3, max_length=MAX_EMAIL_LENGTH)
    code: StrictStr = Field(pattern=r"^\d{6}$")


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: StrictStr = Field(min_length=1, max_length=MAX_EMAIL_LENGTH)


class ForgotPasswordResponse(BaseModel):
    accepted: bool
    message: str
    resend_after_seconds: int


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account: StrictStr = Field(min_length=1, max_length=MAX_EMAIL_LENGTH)
    code: StrictStr = Field(pattern=r"^\d{6}$")
    new_password: StrictStr


class ResetPasswordResponse(BaseModel):
    reset: bool
    sessions_revoked: int


def _normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_PATTERN.match(email):
        raise _http(400, "INVALID_EMAIL", "邮箱格式不正确。")
    return email


def _load_sender() -> EmailSender | None:
    try:
        with pg_transaction() as raw:
            return email_sender_from_settings(BusinessConnection.postgres(raw))
    except SettingsUnavailableError:
        logger.warning("email settings unavailable")
        return None


def _require_sender() -> EmailSender:
    sender = _load_sender()
    if sender is None:
        raise _http(503, "EMAIL_SERVICE_UNAVAILABLE", _SERVICE_UNAVAILABLE_MESSAGE)
    return sender


def _consume_budgets(buckets: list[tuple[str, str, int]], message: str) -> None:
    """在自己的事务里先扣预算：业务事务回滚（错码）不能把扣减一起带走。"""
    with pg_transaction() as conn:
        decisions = [
            consume_rate_limit(
                conn,
                dimension=dimension,
                identifier=identifier,
                limit=limit,
                window_seconds=rate_limit_window_seconds(),
            )
            for dimension, identifier, limit in buckets
        ]
    denied = [item for item in decisions if not item.allowed]
    if denied:
        raise HTTPException(
            429,
            detail={"code": "RATE_LIMITED", "message": message},
            headers={"Retry-After": str(max(item.retry_after_seconds for item in denied))},
        )


def _account_budgets(request: Request, prefix: str, account: str) -> list[tuple[str, str, int]]:
    _, key = highest_device_domain_key()
    return [
        (DIMENSION_LOGIN_IP, prefix + client_ip_from_request(request), login_ip_limit()),
        (
            DIMENSION_LOGIN_ACCOUNT,
            keyed_digest(key, prefix + account.strip().lower()),
            login_account_limit(),
        ),
    ]


def _code_digest_value(code_id: str, code: str) -> str:
    return f"email-code:{code_id}:{code}"


def _issue_code(
    conn: psycopg.Connection, *, user_id: str, purpose: Purpose, target_email: str
) -> str | None:
    """签发新码并作废同用途的旧码；冷却期内回 ``None``。

    调用方必须已持有该用户的行锁，并发的两次签发才不会各自通过冷却检查。
    """
    recent = conn.execute(
        "SELECT 1 FROM customer_email_codes "
        "WHERE user_id = %s AND purpose = %s "
        "AND created_at > clock_timestamp() - make_interval(secs => %s)",
        (user_id, purpose, RESEND_COOLDOWN_SECONDS),
    ).fetchone()
    if recent is not None:
        return None
    conn.execute(
        "DELETE FROM customer_email_codes WHERE user_id = %s AND purpose = %s",
        (user_id, purpose),
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_id = str(uuid.uuid4())
    digests, _ = fingerprint_digests_for(_code_digest_value(code_id, code))
    conn.execute(
        "INSERT INTO customer_email_codes "
        "(id, user_id, purpose, target_email, code_digest, expires_at, created_at) "
        "VALUES (%s, %s, %s, %s, %s, "
        "clock_timestamp() + make_interval(mins => %s), clock_timestamp())",
        (code_id, user_id, purpose, target_email, digests[-1], CODE_TTL_MINUTES),
    )
    return code


def _redeem_code(
    conn: psycopg.Connection,
    *,
    user_id: str,
    purpose: Purpose,
    code: str,
    target_email: str | None = None,
) -> bool:
    """核对并消费验证码；错码只累加次数（调用方须**提交**后再报错）。"""
    row = conn.execute(
        "SELECT id, target_email, code_digest, attempts, "
        "expires_at > clock_timestamp() AS live "
        "FROM customer_email_codes "
        "WHERE user_id = %s AND purpose = %s AND consumed_at IS NULL "
        "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
        (user_id, purpose),
    ).fetchone()
    if row is None:
        return False
    code_id, stored_target, stored_digest, attempts, live = row
    if not live or attempts >= MAX_CODE_ATTEMPTS:
        return False
    digests, _ = fingerprint_digests_for(_code_digest_value(str(code_id), code))
    matches = any(hmac.compare_digest(digest, str(stored_digest)) for digest in digests)
    if not matches or (target_email is not None and target_email != stored_target):
        conn.execute(
            "UPDATE customer_email_codes SET attempts = attempts + 1 WHERE id = %s",
            (code_id,),
        )
        return False
    conn.execute(
        "UPDATE customer_email_codes SET consumed_at = clock_timestamp() WHERE id = %s",
        (code_id,),
    )
    return True


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# ---------------------------------------------------------------------------
# 绑定邮箱（会话围栏内）
# ---------------------------------------------------------------------------


@router.get("/account/email", response_model=EmailStateResponse)
def read_email_state(request: Request, response: Response) -> EmailStateResponse:
    response.headers["Cache-Control"] = "no-store"
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        row = conn.execute(
            "SELECT email, email_verified_at, account_type FROM users WHERE id = %s",
            (ctx.user_id,),
        ).fetchone()
    assert row is not None  # 围栏刚核对过这个账号
    return EmailStateResponse(
        email=row[0],
        verified_at=_timestamp(row[1]),
        can_bind=row[2] == "MASTER",
        service_available=_load_sender() is not None,
    )


@router.post("/account/email/send-code", response_model=CodeSentResponse, status_code=202)
def send_bind_code(
    body: SendBindCodeRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
) -> CodeSentResponse:
    response.headers["Cache-Control"] = "no-store"
    snapshot = _require_customer_snapshot(request)
    email = _normalize_email(body.email)
    sender = _require_sender()
    _, key = highest_device_domain_key()
    _consume_budgets(
        [
            (
                DIMENSION_LOGIN_ACCOUNT,
                keyed_digest(key, _BIND_PREFIX + snapshot.expected_user_id),
                login_account_limit(),
            )
        ],
        "验证码发送过于频繁，请稍后再试。",
    )
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_customer(conn, ctx.user_id)
        row = conn.execute(
            "SELECT email, account_type FROM users WHERE id = %s", (ctx.user_id,)
        ).fetchone()
        assert row is not None
        if row[1] != "MASTER":
            raise _http(
                403,
                "EMAIL_BIND_NOT_ALLOWED",
                "子账号的密码由主账号管理，无需绑定邮箱。",
            )
        if row[0] == email:
            raise _http(409, "EMAIL_ALREADY_BOUND", "该邮箱已绑定当前账号。")
        taken = conn.execute(
            "SELECT 1 FROM users WHERE email = %s AND id <> %s", (email, ctx.user_id)
        ).fetchone()
        if taken is not None:
            raise _http(409, "EMAIL_TAKEN", "该邮箱已绑定其他账号，请换一个邮箱。")
        code = _issue_code(conn, user_id=ctx.user_id, purpose=PURPOSE_BIND, target_email=email)
        if code is None:
            raise HTTPException(
                429,
                detail={"code": "RATE_LIMITED", "message": "验证码已发送，请稍后再重新获取。"},
                headers={"Retry-After": str(RESEND_COOLDOWN_SECONDS)},
            )
    background_tasks.add_task(
        deliver_quietly,
        partial(
            sender.send_code,
            to=email,
            code=code,
            action=_ACTION_LABELS[PURPOSE_BIND],
            minutes=CODE_TTL_MINUTES,
        ),
        kind=PURPOSE_BIND,
    )
    return CodeSentResponse(
        sent=True,
        expires_in_minutes=CODE_TTL_MINUTES,
        resend_after_seconds=RESEND_COOLDOWN_SECONDS,
    )


@router.post("/account/email/verify", response_model=EmailStateResponse)
def verify_bind_code(
    body: VerifyBindCodeRequest, request: Request, response: Response
) -> EmailStateResponse:
    response.headers["Cache-Control"] = "no-store"
    snapshot = _require_customer_snapshot(request)
    email = _normalize_email(body.email)
    _, key = highest_device_domain_key()
    _consume_budgets(
        [
            (
                DIMENSION_LOGIN_ACCOUNT,
                keyed_digest(key, _BIND_PREFIX + "verify:" + snapshot.expected_user_id),
                login_account_limit(),
            )
        ],
        "验证尝试过于频繁，请稍后再试。",
    )
    client_ip, user_agent = _client_context(request)
    verified_at: object = None
    try:
        with fenced_pg_transaction(snapshot) as (conn, ctx):
            _lock_customer(conn, ctx.user_id)
            previous = conn.execute(
                "SELECT email FROM users WHERE id = %s", (ctx.user_id,)
            ).fetchone()
            assert previous is not None
            redeemed = _redeem_code(
                conn,
                user_id=ctx.user_id,
                purpose=PURPOSE_BIND,
                code=body.code,
                target_email=email,
            )
            if redeemed:
                row = conn.execute(
                    "UPDATE users SET email = %s, email_verified_at = clock_timestamp(), "
                    "updated_at = NOW() WHERE id = %s RETURNING email_verified_at",
                    (email, ctx.user_id),
                ).fetchone()
                assert row is not None
                verified_at = row[0]
                _insert_customer_audit(
                    conn,
                    user_id=ctx.user_id,
                    action="customer.email.bound",
                    entity_id=ctx.user_id,
                    metadata={
                        "replaced_previous": previous[0] is not None,
                        "client_ip": client_ip,
                        "user_agent": user_agent,
                    },
                    entity_type="user",
                )
    except UniqueViolation as exc:
        # 发码时核对过占用，但两个账号可能同时验证同一个地址；唯一索引兜底。
        raise _http(409, "EMAIL_TAKEN", "该邮箱已绑定其他账号，请换一个邮箱。") from exc
    if not redeemed:
        # 错码的次数累加已随事务提交，现在才报错。
        raise _http(400, "INVALID_CODE", _INVALID_CODE_MESSAGE)
    return EmailStateResponse(
        email=email,
        verified_at=_timestamp(verified_at),
        can_bind=True,
        service_available=True,
    )


# ---------------------------------------------------------------------------
# 找回密码（无会话）
# ---------------------------------------------------------------------------


def _find_recoverable_account(
    conn: psycopg.Connection, account: str
) -> tuple[str, str, str] | None:
    """(user_id, username, email)；只有能用密码登录、绑了邮箱的主账号才可找回。"""
    account = account.strip()
    column = "email" if "@" in account else "username"
    lookup = account.lower() if column == "email" else account
    row = conn.execute(
        "SELECT id, username, email, registration_source, account_type, parent_user_id "
        f"FROM users WHERE {column} = %s AND role = 'customer' AND is_active = 1 "
        "AND account_type = 'MASTER' AND email IS NOT NULL AND password_hash IS NOT NULL "
        "FOR UPDATE",
        (lookup,),
    ).fetchone()
    if row is None or not password_login_account_ok(
        conn, registration_source=row[3], account_type=row[4], parent_user_id=row[5]
    ):
        return None
    return str(row[0]), str(row[1]), str(row[2])


@router.post("/password/forgot", response_model=ForgotPasswordResponse, status_code=202)
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
) -> ForgotPasswordResponse:
    response.headers["Cache-Control"] = "no-store"
    # 发信没开通是全局事实，不泄露任何账号信息，可以如实回答。
    sender = _require_sender()
    _consume_budgets(
        _account_budgets(request, _FORGOT_PREFIX, body.account),
        "获取验证码过于频繁，请稍后再试。",
    )
    delivery: tuple[str, str] | None = None
    with pg_transaction() as conn:
        found = _find_recoverable_account(conn, body.account)
        if found is not None:
            user_id, _, email = found
            code = _issue_code(conn, user_id=user_id, purpose=PURPOSE_RESET, target_email=email)
            if code is not None:
                delivery = (email, code)
    if delivery is not None:
        email, code = delivery
        background_tasks.add_task(
            deliver_quietly,
            partial(
                sender.send_code,
                to=email,
                code=code,
                action=_ACTION_LABELS[PURPOSE_RESET],
                minutes=CODE_TTL_MINUTES,
            ),
            kind=PURPOSE_RESET,
        )
    return ForgotPasswordResponse(
        accepted=True,
        message=FORGOT_ACCEPTED_MESSAGE,
        resend_after_seconds=RESEND_COOLDOWN_SECONDS,
    )


@router.post("/password/reset", response_model=ResetPasswordResponse)
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
) -> ResetPasswordResponse:
    response.headers["Cache-Control"] = "no-store"
    # 哈希在事务外：策略不过关就不必拿行锁，scrypt 也不该占着池连接。
    try:
        encoded = hash_password(body.new_password)
    except PasswordPolicyError as exc:
        raise _http(400, "WEAK_PASSWORD", str(exc)) from exc
    _consume_budgets(
        _account_budgets(request, _RESET_PREFIX, body.account),
        "重置尝试过于频繁，请稍后再试。",
    )
    request_id = _request_id(request)
    client_ip, user_agent = _client_context(request)
    notice: tuple[str, str] | None = None
    sessions_revoked = 0
    with pg_transaction() as conn:
        found = _find_recoverable_account(conn, body.account)
        if found is not None:
            user_id, username, email = found
            if _redeem_code(conn, user_id=user_id, purpose=PURPOSE_RESET, code=body.code):
                conn.execute(
                    "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                    (encoded, user_id),
                )
                sessions_revoked = _live_session_count(conn, user_id)
                revoke_session(
                    conn,
                    user_id=user_id,
                    actor_user_id=user_id,
                    reason=REASON_PASSWORD_RESET,
                    request_id=request_id,
                    now_iso=transaction_now_iso(conn),
                )
                _insert_customer_audit(
                    conn,
                    user_id=user_id,
                    action="customer.password.reset",
                    entity_id=user_id,
                    metadata={
                        "channel": "email",
                        "sessions_revoked": sessions_revoked,
                        "client_ip": client_ip,
                        "user_agent": user_agent,
                    },
                    entity_type="user",
                )
                notice = (email, username)
    if notice is None:
        # 未知账号与错码同答：不回答「这个账号存不存在」。
        raise _http(400, "INVALID_CODE", _INVALID_CODE_MESSAGE)
    sender = _load_sender()
    if sender is not None:
        email, username = notice
        occurred_at = datetime.now(UTC).astimezone(_BEIJING).strftime("%Y-%m-%d %H:%M")
        background_tasks.add_task(
            deliver_quietly,
            partial(
                sender.send_password_reset_notice,
                to=email,
                username=username,
                occurred_at=occurred_at,
            ),
            kind="password_reset_notice",
        )
    return ResetPasswordResponse(reset=True, sessions_revoked=sessions_revoked)
