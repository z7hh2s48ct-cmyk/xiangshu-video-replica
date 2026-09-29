"""客户标注：标签 / 备注 / 负责人（方案 P2-3）。

运营对客户的长期协作信息——这个客户归谁跟、是什么类型（VIP / 试用 / 风险）、
有什么上下文要交代——此前只能散落在外部表格里。本模块把三件事收在一张
``customer_annotations`` 表（20260929T1000 迁移）上：

- **标签**：短词列表（≤10 个、每个 ≤24 字符），列表页 chips 展示；
- **备注**：一段自由文本（≤2000 字符），详情页展示与编辑；
- **负责人**：一个启用中的管理员账号（``users.role = 'admin'``），表示
  「谁在跟这个客户」；审计员只读、不成为负责人，所以候选列表只列 admin。

写入走与调账 / 定价相同的四段契约（dev doc §15）：真实管理员会话（审计员
只读，403 AUDITOR_READ_ONLY）、Idempotency-Key、confirm=true、非空 reason，
旧值 / 新值一并写进 ``audit_logs``（``customer_annotation.update``）。

空标注不落行：三个字段全空时 PUT 删除整行，列表回到「无标注」状态——
运营清空后不该留一行空壳。读取降级为零值 payload（无行 = 无标注），
列表页因此不需要区分「行不存在」与「字段为空」。
"""

from __future__ import annotations

import json
import logging
import uuid

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import ConfigDict

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_customer_routes import _write_with_idempotency
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.db_pg import MissingDatabaseConfigError, pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-customer-annotations"])
logger = logging.getLogger(__name__)

MAX_TAGS = 10
MAX_TAG_CHARS = 24
MAX_NOTE_CHARS = 2000

_UNAVAILABLE_CODE = "CUSTOMER_ANNOTATION_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "客户标注功能需要 PostgreSQL 运行时。"


class CustomerAnnotationUpdateRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    tags: list[str] = []
    note: str = ""
    owner_user_id: str | None = None


def _require_customer(conn: psycopg.Connection, user_id: str) -> None:
    if conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone() is None:
        raise _http(404, "USER_NOT_FOUND", "客户不存在。")


def _normalize_tags(raw: list[str]) -> list[str]:
    """去空白 / 去空项 / 保序去重，再执行上限校验。

    运营在输入框里按分隔符录入，重复与手滑空项很常见：原样落库会让 chips
    出现两个一模一样的词。归一化在服务端做一次，前端只管展示。
    """
    tags: list[str] = []
    for item in raw:
        tag = item.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_CHARS:
            raise _http(
                400,
                "CUSTOMER_ANNOTATION_VALIDATION_FAILED",
                f"单个标签不能超过 {MAX_TAG_CHARS} 个字符。",
            )
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise _http(
            400,
            "CUSTOMER_ANNOTATION_VALIDATION_FAILED",
            f"标签最多 {MAX_TAGS} 个。",
        )
    return tags


def _annotation_payload(
    conn: psycopg.Connection,
    *,
    user_id: str,
    request_id: str | None = None,
) -> dict[str, object]:
    """读一条标注；无行即零值（None 与空字段对前端是同一件事）。

    ``tags_json`` 在 psycopg 下通常已是 list；历史或异常路径若回来字符串
    就解析一次，坏值降级为空列表，不把 500 抛给详情页。
    """
    row = conn.execute(
        """
        SELECT ca.tags_json, ca.note, ca.owner_user_id,
               COALESCE(owner.username, ''), ca.updated_by_user_id, ca.updated_at
        FROM customer_annotations ca
        LEFT JOIN users owner ON owner.id = ca.owner_user_id
        WHERE ca.user_id = %s
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return {
            "user_id": user_id,
            "tags": [],
            "note": "",
            "owner_user_id": "",
            "owner_username": "",
            "updated_by_user_id": "",
            "updated_at": "",
            "request_id": request_id,
        }
    raw_tags = row[0]
    if isinstance(raw_tags, str):
        try:
            raw_tags = json.loads(raw_tags)
        except ValueError:
            raw_tags = []
    tags = [str(tag) for tag in raw_tags] if isinstance(raw_tags, list) else []
    return {
        "user_id": user_id,
        "tags": tags,
        "note": str(row[1]),
        "owner_user_id": "" if row[2] is None else str(row[2]),
        "owner_username": str(row[3]),
        "updated_by_user_id": str(row[4]),
        "updated_at": row[5].isoformat() if row[5] is not None else "",
        "request_id": request_id,
    }


@router.get("/customers/owner-candidates")
def list_owner_candidates(actor: AdminReader) -> dict[str, object]:
    """负责人候选：启用中的管理员账号（审计员只读，不列入）。"""
    del actor
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                "SELECT id, username, display_name FROM users "
                "WHERE role = 'admin' AND is_active = 1 "
                "ORDER BY username, id"
            ).fetchall()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc
    return {
        "items": [
            {
                "user_id": str(row[0]),
                "username": str(row[1]),
                "display_name": str(row[2]),
            }
            for row in rows
        ]
    }


@router.get("/customers/{user_id}/annotation")
def read_customer_annotation(user_id: str, actor: AdminReader) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            _require_customer(conn, user_id)
            return _annotation_payload(conn, user_id=user_id)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE) from exc


@router.put("/customers/{user_id}/annotation")
def update_customer_annotation(
    user_id: str,
    body: CustomerAnnotationUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """整体替换一条标注；三字段全空即删除整行（空标注不落行）。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        _require_customer(conn, user_id)
        tags = _normalize_tags(body.tags)
        note = body.note.strip()
        if len(note) > MAX_NOTE_CHARS:
            raise _http(
                400,
                "CUSTOMER_ANNOTATION_VALIDATION_FAILED",
                f"备注不能超过 {MAX_NOTE_CHARS} 个字符。",
            )
        owner_user_id = (body.owner_user_id or "").strip() or None
        if owner_user_id is not None:
            owner = conn.execute(
                "SELECT 1 FROM users WHERE id = %s AND role = 'admin' AND is_active = 1",
                (owner_user_id,),
            ).fetchone()
            if owner is None:
                raise _http(
                    400,
                    "CUSTOMER_ANNOTATION_VALIDATION_FAILED",
                    "负责人必须是启用中的管理员账号。",
                )

        before = _annotation_payload(conn, user_id=user_id)
        if not tags and not note and owner_user_id is None:
            conn.execute(
                "DELETE FROM customer_annotations WHERE user_id = %s",
                (user_id,),
            )
        else:
            # 整体替换语义：三列一律覆盖，不做字段级差异合并——运营在表单里
            # 看到什么，库里就是什么。
            conn.execute(
                """
                INSERT INTO customer_annotations
                    (user_id, tags_json, note, owner_user_id,
                     updated_by_user_id, updated_at)
                VALUES (%s, %s::jsonb, %s, %s, %s, clock_timestamp())
                ON CONFLICT (user_id) DO UPDATE
                SET tags_json = EXCLUDED.tags_json,
                    note = EXCLUDED.note,
                    owner_user_id = EXCLUDED.owner_user_id,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = clock_timestamp()
                """,
                (
                    user_id,
                    json.dumps(tags, ensure_ascii=False),
                    note,
                    owner_user_id,
                    actor.user_id,
                ),
            )

        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'customer_annotation.update', 'customer_annotation', %s, %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                user_id,
                json.dumps(
                    {
                        "old_tags": before["tags"],
                        "new_tags": tags,
                        "old_owner_user_id": before["owner_user_id"],
                        "new_owner_user_id": owner_user_id or "",
                        # 备注原文不入审计：协作备注可能含客户隐私，留长度与
                        # 新旧差异信号即可（审计要回答「谁改了什么时间」，
                        # 不是「改成什么」——现文在标注表里随时可读）。
                        "old_note_chars": len(str(before["note"])),
                        "new_note_chars": len(note),
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "customer annotation updated: user=%s actor=%s tags=%d owner=%s request=%s",
            user_id,
            actor.user_id,
            len(tags),
            owner_user_id or "-",
            request_id,
        )
        return _annotation_payload(conn, user_id=user_id, request_id=request_id)

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )
