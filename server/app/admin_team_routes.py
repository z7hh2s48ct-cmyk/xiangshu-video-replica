"""团队与权限：成员列表 / 新增 / 停用启用 / 重置密码（方案 P2-4）。

管理端此前只有「初始化时带出来的单个管理员」与恢复流程，没有任何界面化
的团队成员管理——新增同事要手工 INSERT、离职停用要手工改库、忘记密码要
发一次性恢复凭证。本模块把四件事收在 ``/api/control/team`` 下：

- **列表**：admin / auditor 两种角色（customer 与 employee 不属于团队）；
- **新增**：建 users + wallets + admin_password_credentials 三件套，与
  ``bootstrap.py`` 的首个管理员同构（账号没有钱包行就不是完整账号）；
- **停用/启用**：软删语义——停用同时吊销该成员全部管理会话，已登录的
  人会立刻被踢出；不提供物理删除（审计与历史 idempotency 都引用 user_id）；
- **重置密码**：由超管为成员设置新密码（成员自己的密码恢复走一次性凭证
  流程），同样吊销旧会话。

全部端点都是超管专属（``SuperAdminWriter`` / ``SuperAdminReader``）：团队
构成与成员状态是管理信息，普通 admin 不可见。写端点是超管对成员的「代操作」，
不走自服务流程，但仍遵守共享写契约（Idempotency-Key + confirm + reason）。

安全边界（全部 fail-closed）：

- 不能停用自己、不能改自己的超管标记——防超管手滑把自己锁在门外；
- 不能移除/停用最后一个活跃超管——否则没有账号能再进入团队页；
- 重置密码拒绝把目标指向自己：自服务通道是一次性凭证流程，不从这里绕。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Literal

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import ConfigDict, Field

from app.admin_auth_routes import (
    MAX_ADMIN_PASSWORD_LENGTH,
    MIN_ADMIN_PASSWORD_LENGTH,
    SuperAdminReader,
    SuperAdminWriter,
    hash_admin_password,
)
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.admin_write_contract import transaction_now_iso, write_with_idempotency
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control/team", tags=["admin-team"])
logger = logging.getLogger(__name__)

# 团队成员 = 管理端两种角色。customer / employee 走各自的产品线，不进这里。
_TEAM_ROLES = ("admin", "auditor")

_UNAVAILABLE_CODE = "TEAM_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "团队成员管理需要 PostgreSQL 运行时。"

# 列表与单行共用的投影：用户基础列 + 密码凭据存在性 + 最近一次登录。
# ``admin_sessions.created_at`` 是 ISO 文本，MAX 的字典序即时间序（同区内
# 写入的都是 UTC 偏移，仓库全量路径如此）。
_MEMBER_SELECT = (
    "SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.is_super_admin, "
    "       CASE WHEN c.user_id IS NULL THEN 0 ELSE 1 END, "
    "       COALESCE((SELECT MAX(s.created_at) FROM admin_sessions s "
    "                 WHERE s.actor_user_id = u.id), ''), "
    "       COALESCE(u.created_at, '') "
    "FROM users u "
    "LEFT JOIN admin_password_credentials c ON c.user_id = u.id "
)


def _member_payload(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "user_id": str(row[0]),
        "username": str(row[1]),
        "display_name": str(row[2]),
        "role": str(row[3]),
        "is_active": row[4] == 1,
        "is_super_admin": row[5] == 1,
        "has_password": row[6] == 1,
        "last_login_at": str(row[7]),
        "created_at": str(row[8]),
    }


def _load_member(conn: psycopg.Connection, user_id: str) -> dict[str, object]:
    row = conn.execute(_MEMBER_SELECT + "WHERE u.id = %s", (user_id,)).fetchone()
    if row is None:
        raise _http(404, "TEAM_MEMBER_NOT_FOUND", "团队成员不存在。")
    return _member_payload(row)


def _require_team_target(conn: psycopg.Connection, user_id: str) -> tuple[int, int]:
    """目标必须是团队成员；返回 (is_active, is_super_admin) 供保护逻辑判定。"""
    row = conn.execute(
        "SELECT is_active, is_super_admin FROM users WHERE id = %s AND role = ANY(%s)",
        (user_id, list(_TEAM_ROLES)),
    ).fetchone()
    if row is None:
        raise _http(404, "TEAM_MEMBER_NOT_FOUND", "团队成员不存在。")
    return int(row[0]), int(row[1])


def _revoke_all_sessions(conn: psycopg.Connection, user_id: str, *, revoked_at: str) -> int:
    """吊销目标成员的全部管理会话；返回被吊销的数量（写进审计）。"""
    return conn.execute(
        "UPDATE admin_sessions SET revoked_at = %s WHERE actor_user_id = %s AND revoked_at IS NULL",
        (revoked_at, user_id),
    ).rowcount


def _other_active_super_admins(conn: psycopg.Connection, exclude_user_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM users "
        "WHERE role = 'admin' AND is_active = 1 AND is_super_admin = 1 AND id <> %s",
        (exclude_user_id,),
    ).fetchone()
    return int(row[0]) if row is not None else 0


class TeamMemberCreateRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    role: Literal["admin", "auditor"]
    password: str = Field(
        min_length=MIN_ADMIN_PASSWORD_LENGTH, max_length=MAX_ADMIN_PASSWORD_LENGTH
    )


class TeamMemberUpdateRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    is_active: bool | None = None
    is_super_admin: bool | None = None


class TeamMemberPasswordRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(
        min_length=MIN_ADMIN_PASSWORD_LENGTH, max_length=MAX_ADMIN_PASSWORD_LENGTH
    )


@router.get("/members")
def list_team_members(actor: SuperAdminReader) -> dict[str, object]:
    """团队成员列表（超管专属）：含角色 / 启用状态 / 是否超管 / 最近登录。"""
    del actor
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                _MEMBER_SELECT + "WHERE u.role = ANY(%s) ORDER BY u.username, u.id",
                (list(_TEAM_ROLES),),
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {"items": [_member_payload(row) for row in rows]}


@router.post("/members", status_code=201)
def create_team_member(
    body: TeamMemberCreateRequest,
    request: Request,
    response: Response,
    actor: SuperAdminWriter,
) -> dict[str, object]:
    """新增团队成员：users + wallets + 密码凭据三件套一次建齐。"""
    username = body.username.strip()
    display_name = body.display_name.strip()
    try:
        password_hash = hash_admin_password(body.password)
    except ValueError as exc:
        raise _http(
            400,
            "TEAM_MEMBER_VALIDATION_FAILED",
            f"密码需为 {MIN_ADMIN_PASSWORD_LENGTH} 到 {MAX_ADMIN_PASSWORD_LENGTH} "
            "个字符，且不能全为空白。",
        ) from exc

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if conn.execute("SELECT 1 FROM users WHERE username = %s", (username,)).fetchone():
            raise _http(409, "TEAM_MEMBER_USERNAME_TAKEN", "该登录名已被使用。")
        user_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO users (id, username, display_name, role, is_active, is_super_admin) "
            "VALUES (%s, %s, %s, %s, 1, 0)",
            (user_id, username, display_name, body.role),
        )
        # 账号的完整性 = 用户行 + 钱包行 + 管理密码凭据；缺钱包的账号在
        # 计费/钱包路径上不是「完整账号」，与 bootstrap 首个管理员同构。
        conn.execute("INSERT INTO wallets (user_id) VALUES (%s)", (user_id,))
        conn.execute(
            "INSERT INTO admin_password_credentials "
            "(user_id, password_hash, credential_version, password_changed_at) "
            "VALUES (%s, %s, 1, clock_timestamp())",
            (user_id, password_hash),
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'team.member.create', 'team_member', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                user_id,
                json.dumps(
                    {
                        "username": username,
                        "role": body.role,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "team member created: user=%s username=%s role=%s actor=%s request=%s",
            user_id,
            username,
            body.role,
            actor.user_id,
            request_id,
        )
        return _load_member(conn, user_id) | {"request_id": request_id}

    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=201,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )


@router.patch("/members/{user_id}")
def update_team_member(
    user_id: str,
    body: TeamMemberUpdateRequest,
    request: Request,
    response: Response,
    actor: SuperAdminWriter,
) -> dict[str, object]:
    """更新成员（显示名 / 启用状态 / 超管标记）；停用即吊销其全部会话。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if body.display_name is None and body.is_active is None and body.is_super_admin is None:
            raise _http(400, "TEAM_MEMBER_VALIDATION_FAILED", "没有需要更新的字段。")
        current_active, current_super = _require_team_target(conn, user_id)
        new_active = current_active if body.is_active is None else int(body.is_active)
        new_super = current_super if body.is_super_admin is None else int(body.is_super_admin)

        if user_id == actor.user_id:
            # 自我保护：停用自己 = 立刻把自己踢出；改自己的超管标记（两个
            # 方向）都可能造成「最后一个超管把自己降级」的锁死路径。
            if new_active == 0:
                raise _http(400, "CANNOT_DEACTIVATE_SELF", "不能停用自己的账号。")
            if new_super != current_super:
                raise _http(400, "CANNOT_CHANGE_OWN_SUPER_FLAG", "不能修改自己的超管标记。")

        losing_super = (
            current_active == 1 and current_super == 1 and (new_super == 0 or new_active == 0)
        )
        if losing_super and _other_active_super_admins(conn, user_id) == 0:
            raise _http(
                400,
                "LAST_SUPER_ADMIN_REQUIRED",
                "至少需要保留一个启用中的超级管理员。",
            )

        before = _load_member(conn, user_id)
        new_display_name = (
            str(before["display_name"]) if body.display_name is None else body.display_name.strip()
        )
        db_now = transaction_now_iso(conn)
        revoked_sessions = 0
        if new_active == 0 and current_active == 1:
            revoked_sessions = _revoke_all_sessions(conn, user_id, revoked_at=db_now)
        conn.execute(
            "UPDATE users SET display_name = %s, is_active = %s, is_super_admin = %s WHERE id = %s",
            (new_display_name, new_active, new_super, user_id),
        )
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'team.member.update', 'team_member', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                user_id,
                json.dumps(
                    {
                        "old_display_name": before["display_name"],
                        "new_display_name": new_display_name,
                        "old_is_active": before["is_active"],
                        "new_is_active": bool(new_active),
                        "old_is_super_admin": before["is_super_admin"],
                        "new_is_super_admin": bool(new_super),
                        "revoked_sessions": revoked_sessions,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "team member updated: user=%s active=%s super=%s revoked_sessions=%d "
            "actor=%s request=%s",
            user_id,
            new_active,
            new_super,
            revoked_sessions,
            actor.user_id,
            request_id,
        )
        return _load_member(conn, user_id) | {"request_id": request_id}

    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )


@router.post("/members/{user_id}/password")
def reset_team_member_password(
    user_id: str,
    body: TeamMemberPasswordRequest,
    request: Request,
    response: Response,
    actor: SuperAdminWriter,
) -> dict[str, object]:
    """超管为成员设置新密码；旧密码立即失效，已登录会话全部吊销。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            # 自服务通道是一次性凭证的恢复流程（/api/control/admin/password），
            # 超管对自己的口令也走那里——这里拒绝，保证两条路径不混用。
            raise _http(
                400,
                "CANNOT_RESET_OWN_PASSWORD",
                "不能通过本页重置自己的密码，请使用账号恢复流程。",
            )
        _require_team_target(conn, user_id)
        try:
            password_hash = hash_admin_password(body.password)
        except ValueError as exc:
            raise _http(
                400,
                "TEAM_MEMBER_VALIDATION_FAILED",
                f"密码需为 {MIN_ADMIN_PASSWORD_LENGTH} 到 {MAX_ADMIN_PASSWORD_LENGTH} "
                "个字符，且不能全为空白。",
            ) from exc
        db_now = transaction_now_iso(conn)
        conn.execute(
            "INSERT INTO admin_password_credentials "
            "(user_id, password_hash, credential_version, password_changed_at) "
            "VALUES (%s, %s, 1, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "password_hash = EXCLUDED.password_hash, "
            "credential_version = admin_password_credentials.credential_version + 1, "
            "password_changed_at = EXCLUDED.password_changed_at",
            (user_id, password_hash, db_now),
        )
        # 密码一换，旧会话就是「旧凭据签发的会话」——全部吊销，与
        # recover_admin_password 的语义一致。
        revoked_sessions = _revoke_all_sessions(conn, user_id, revoked_at=db_now)
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'team.member.password_reset', 'team_member', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                user_id,
                # 不记密码哈希/版本外的任何凭据材料；审计回答「谁在何时重置了谁」。
                json.dumps(
                    {
                        "revoked_sessions": revoked_sessions,
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "team member password reset: user=%s revoked_sessions=%d actor=%s request=%s",
            user_id,
            revoked_sessions,
            actor.user_id,
            request_id,
        )
        return {"user_id": user_id, "revoked_sessions": revoked_sessions, "request_id": request_id}

    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )
