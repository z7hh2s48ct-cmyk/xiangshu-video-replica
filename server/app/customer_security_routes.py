"""CW-062 批次 4 / 个人中心审计方案 E —— 账号安全的客户自助写路径。

本模块只承担两件事，都在**会话围栏**内（无会话令牌即 401；刻意不接受
``xsk_live_`` 独立泳道——凭据类动作必须有人在场）：

1. ``POST /api/customer/account/password/change``：校验当前密码 → 换新哈希 →
   撤销活跃会话 → 落审计。当前密码错误与登录同码同文案（401
   ``INVALID_CREDENTIALS``），新密码策略原样沿用 ``password_hashing``
   （6–128，scrypt），不引入第二套策略。**改密即退出所有设备**：本仓库是单会话
   架构（``customer_session_state`` 主键即 ``user_id``，029 §11.3「one live
   session per user」），撤销活跃会话就把所有端打回登录页，因此没有「除当前
   会话外」这种分支可言。
2. ``GET /api/customer/sessions/history``：只读既有 append-only 表
   ``customer_session_events``（join ``customer_devices`` 取设备名/平台），倒序。
   该表没有 IP / User-Agent 列，所以这里如实只回时间、事件、设备、平台、原因；
   把 IP/UA 落库需要新列或新表（迁移），不在本批范围内。

两处刻意不建的东西：

- **不建 ``security_event_log``**：``audit_logs``（001 起）已具备事件审计能力，
  客户泳道也已在写（``customer.password.initialized`` /
  ``customer.sub_account.password.reset``），再建一张表只会制造第二份真相，且
  ``BIGSERIAL``/``JSONB``/``INET`` 都违反本仓库约定（id 全为 Python 侧 uuid、
  JSON 全存 TEXT）。
- **不新增速率维度**：防爆破复用既有 ``login:account`` 维度，identifier 带
  ``password-change:`` 前缀（与登录的 ``password:`` 计数器互不干扰）。新维度要改
  ``security_auth_failures`` 的 dimension CHECK，那就是一次迁移。

审计的 IP / User-Agent 只落在 ``audit_logs.metadata_json``（运维可见的审计面），
不落进 append-only 的会话事件表。
"""

from __future__ import annotations

import logging
import uuid

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StrictStr

from app.admin_write_contract import transaction_now_iso
from app.api_errors import http_error as _http
from app.api_key_routes import (
    _insert_customer_audit,
    _lock_customer,
    _require_customer_snapshot,
)
from app.customer_device_service import highest_device_domain_key, keyed_digest
from app.customer_fence import fenced_pg_transaction
from app.customer_session_service import (
    REASON_ALL_DEVICES_REVOKED,
    REASON_PASSWORD_CHANGED,
    revoke_session,
)
from app.db_pg import pg_transaction
from app.password_hashing import PasswordPolicyError, hash_password, verify_password
from app.security_rate_limit import (
    DIMENSION_LOGIN_ACCOUNT,
    client_ip_from_request,
    consume_rate_limit,
    login_account_limit,
    rate_limit_window_seconds,
    record_auth_failure,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/customer", tags=["customer-security"])

# 一次最多回 100 条登录事件：这是给人看的自救记录，不是导出通道。
MAX_HISTORY_LIMIT = 100
DEFAULT_HISTORY_LIMIT = 20

# 审计里的 User-Agent 只留可读前缀，避免把超长 header 落库。
MAX_USER_AGENT_LENGTH = 200

# 改密预算的 identifier 前缀：与登录共用 ``login:account`` 维度但不共用计数器。
CHANGE_BUDGET_PREFIX = "password-change:"

# 心跳不是事件：它每分钟一条，登录记录列表要把它挡在外面（029 的 event 取值之一）。
HEARTBEAT_EVENT = "HEARTBEAT"


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: StrictStr
    new_password: StrictStr


class PasswordChangeResponse(BaseModel):
    changed: bool
    # 被撤销的活跃会话数（单会话架构下是 0 或 1，见模块 docstring）。
    sessions_revoked: int


class SessionRevokeResponse(BaseModel):
    """被下线的在线会话数（0 = 本来就没有在线会话）。"""

    revoked_sessions: int


class SessionEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    occurred_at: str
    event: str
    device_name: str | None
    platform: str | None
    reason: str | None


class SessionHistoryResponse(BaseModel):
    items: list[SessionEventItem]
    total: int


class _PasswordMismatch(Exception):
    """内部哨兵：当前密码不匹配。

    用它把「事务内发现密码错」与「事务外落失败记录」分开——失败记录必须在业务
    事务提交/回滚**之后**、用自己的连接写，不能在事务里再开一条
    （见 ``_record_change_failure`` 的说明）。
    """


def _request_id(request: Request) -> str:
    """事件/审计的追踪 id：调用方的 Idempotency-Key，缺省则现生成。

    与子账号自助泳道（``customer_sub_account_routes._request_id``）同一口径：重试
    带同一个键，审计里就能看出是「一次操作的重试」而不是两次操作。键是调用方给的
    文本且会落进 append-only 列，所以先剥掉控制字符再截断。
    """
    header = request.headers.get("Idempotency-Key", "")
    cleaned = "".join(char for char in header if char.isprintable()).strip()
    return cleaned[:200] if cleaned else str(uuid.uuid4())


def _live_session_count(conn: psycopg.Connection, user_id: str) -> int:
    """真正在线的会话数：租约还没过期的才算。

    用「在线」而不是「行数」回答用户，是因为 ``revoke_session`` 会连带把已经
    过期的行再推一次 epoch——那是数据库里的事实，不是「我下线了几台设备」。
    """
    row = conn.execute(
        "SELECT count(*) FROM customer_session_state "
        "WHERE user_id = %s AND lease_until::timestamptz > clock_timestamp()",
        (user_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _client_context(request: Request) -> tuple[str, str]:
    """审计用的来源信息（只进 audit_logs，不进会话事件表）。"""
    user_agent = request.headers.get("User-Agent", "")[:MAX_USER_AGENT_LENGTH]
    return client_ip_from_request(request), user_agent


def _timestamp(value: object) -> str:
    """把 timestamptz 列转成 ISO 8601（``str(datetime)`` 给的是空格分隔的非 ISO 形式，
    只有 V8 的非标准回退解析认它，其它引擎不保证；仓库既有口径就是 ``isoformat()``）。"""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


def _consume_change_budget(session_user_id: str) -> None:
    """先花掉一次改密预算，再进业务事务。

    必须在**自己的事务**里扣预算：业务事务在「当前密码错误」时会回滚，把同事务
    内的扣减一起带走，防爆破就成了摆设（登录泳道是同样的理由先扣后校验）。
    identifier 用 ``keyed_digest`` 派生——计数器里永不出现明文标识。
    """
    _, key = highest_device_domain_key()
    with pg_transaction() as conn:
        decision = consume_rate_limit(
            conn,
            dimension=DIMENSION_LOGIN_ACCOUNT,
            identifier=keyed_digest(key, CHANGE_BUDGET_PREFIX + session_user_id),
            limit=login_account_limit(),
            window_seconds=rate_limit_window_seconds(),
        )
    if not decision.allowed:
        raise HTTPException(
            429,
            detail={
                "code": "RATE_LIMITED",
                "message": "改密尝试过于频繁，请稍后再试。",
            },
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )


def _record_change_failure(session_user_id: str, request_id: str) -> None:
    """记录一次「当前密码错误」，供暴力破解告警聚合（尽力而为，失败只告警）。

    **必须在业务事务之外调用**：这里要开自己的连接，如果从 ``fenced_pg_transaction``
    内部调用，一次改密会同时占两条池连接并仍握着 ``users`` 的行锁——恰好在这种
    「大量错误密码」的场景下先把连接池耗光，而池耗尽又会被下面的 except 吞掉，
    于是最该留下记录的失败一条也留不下（登录泳道同样把失败记录放在事务之外）。
    """
    try:
        _, key = highest_device_domain_key()
        with pg_transaction() as conn:
            record_auth_failure(
                conn,
                dimension=DIMENSION_LOGIN_ACCOUNT,
                identifier=keyed_digest(key, CHANGE_BUDGET_PREFIX + session_user_id),
                request_id=request_id,
            )
    except Exception as audit_error:  # pragma: no cover - 审计不可用不该改答案
        logger.warning(
            "password-change failure audit unavailable (%s)",
            type(audit_error).__name__,
        )


@router.post("/account/password/change", response_model=PasswordChangeResponse)
def change_password(
    body: ChangePasswordRequest, request: Request, response: Response
) -> PasswordChangeResponse:
    """用当前密码换新密码，并让所有端的活跃会话立即失效。

    哈希在事务外完成：策略不过关就没必要拿行锁，也不该把一次拒绝变成一次锁等待。
    """
    snapshot = _require_customer_snapshot(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        encoded = hash_password(body.new_password)
    except PasswordPolicyError as exc:
        raise _http(400, "WEAK_PASSWORD", str(exc)) from exc

    _consume_change_budget(snapshot.expected_user_id)

    request_id = _request_id(request)
    client_ip, user_agent = _client_context(request)
    try:
        with fenced_pg_transaction(snapshot) as (conn, ctx):
            _lock_customer(conn, ctx.user_id)
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = %s", (ctx.user_id,)
            ).fetchone()
            assert row is not None  # 上面的行锁刚读到它
            current_hash = row[0]
            if current_hash is None:
                # 老激活账号还没设过密码——那走的是「设置登录信息」，不是改密。
                raise _http(
                    409,
                    "PASSWORD_NOT_SET",
                    "该账号尚未设置登录密码，请先在账号设置中设置登录信息。",
                )
            if not verify_password(body.current_password, current_hash):
                raise _PasswordMismatch

            conn.execute(
                "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
                (encoded, ctx.user_id),
            )
            # 先数在线会话再撤销：revoke_session 只回答「有没有撤到」，「下线了几台」
            # 是给用户的反馈，读一次比改一个被六处调用的共享函数的返回值更稳。
            sessions_revoked = _live_session_count(conn, ctx.user_id)
            revoke_session(
                conn,
                user_id=ctx.user_id,
                actor_user_id=ctx.user_id,
                reason=REASON_PASSWORD_CHANGED,
                request_id=request_id,
                now_iso=transaction_now_iso(conn),
            )
            _insert_customer_audit(
                conn,
                user_id=ctx.user_id,
                action="customer.password.changed",
                entity_id=ctx.user_id,
                metadata={
                    "sessions_revoked": sessions_revoked,
                    "client_ip": client_ip,
                    "user_agent": user_agent,
                },
                entity_type="user",
            )
    except _PasswordMismatch:
        # 事务已经回滚（行锁已释放），现在才在独立连接上落失败记录。
        _record_change_failure(snapshot.expected_user_id, request_id)
        raise _http(401, "INVALID_CREDENTIALS", "当前密码不正确。") from None
    return PasswordChangeResponse(changed=True, sessions_revoked=sessions_revoked)


@router.post("/sessions/revoke-all", response_model=SessionRevokeResponse)
def revoke_all_sessions(request: Request, response: Response) -> SessionRevokeResponse:
    """立即下线本账号的全部登录（「退出所有设备」）。

    与改密是两条独立的自救路径：怀疑凭据泄漏时先改密，怀疑只是某台设备被人
    用着时先下线。**这条会把调用方自己也下线**——单会话架构下没有「除当前
    会话外」可言（见模块 docstring），前端据此把用户带回登录页。
    """
    response.headers["Cache-Control"] = "no-store"
    snapshot = _require_customer_snapshot(request)
    request_id = _request_id(request)
    client_ip, user_agent = _client_context(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        _lock_customer(conn, ctx.user_id)
        revoked_sessions = _live_session_count(conn, ctx.user_id)
        revoke_session(
            conn,
            user_id=ctx.user_id,
            actor_user_id=ctx.user_id,
            reason=REASON_ALL_DEVICES_REVOKED,
            request_id=request_id,
            now_iso=transaction_now_iso(conn),
        )
        _insert_customer_audit(
            conn,
            user_id=ctx.user_id,
            action="customer.sessions.revoked_all",
            entity_id=ctx.user_id,
            metadata={
                "revoked_sessions": revoked_sessions,
                "client_ip": client_ip,
                "user_agent": user_agent,
            },
            entity_type="user",
        )
    return SessionRevokeResponse(revoked_sessions=revoked_sessions)


@router.get("/sessions/history", response_model=SessionHistoryResponse)
def session_history(
    request: Request,
    response: Response,
    limit: int = Query(default=DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
) -> SessionHistoryResponse:
    """调用方自己的登录/会话事件，最新在前（只含非凭据字段）。

    心跳被排除在外：它每分钟一条，混进来会把真正的事件（登录、切换设备、退出、
    超时）冲走，而这张表存在的意义恰恰是让用户看见那几个事件。
    """
    response.headers["Cache-Control"] = "no-store"
    snapshot = _require_customer_snapshot(request)
    with fenced_pg_transaction(snapshot) as (conn, ctx):
        rows = conn.execute(
            "SELECT e.occurred_at, e.event, d.display_name, d.platform, e.reason "
            "FROM customer_session_events e "
            "LEFT JOIN customer_devices d ON d.id = e.device_id "
            "WHERE e.user_id = %s AND e.event <> %s "
            "ORDER BY e.occurred_at DESC, e.id DESC "
            "LIMIT %s",
            (ctx.user_id, HEARTBEAT_EVENT, limit),
        ).fetchall()
        total_row = conn.execute(
            "SELECT count(*) FROM customer_session_events WHERE user_id = %s AND event <> %s",
            (ctx.user_id, HEARTBEAT_EVENT),
        ).fetchone()
    total = int(total_row[0]) if total_row is not None else 0
    return SessionHistoryResponse(
        items=[
            SessionEventItem(
                occurred_at=_timestamp(row[0]),
                event=str(row[1]),
                device_name=row[2],
                platform=row[3],
                reason=row[4],
            )
            for row in rows
        ],
        total=total,
    )
