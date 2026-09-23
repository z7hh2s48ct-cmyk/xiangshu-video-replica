"""C7 studio drafts domain: cloud persistence for the V1.4 copy workshop.

The copy workshop keeps its working draft (whole ``StudioDraft`` snapshot as
opaque JSON) and an explicitly saved script list. Drafts are strictly
user-scoped — no project access checks, because drafts may exist before any
project does (viral-video / persona entry points). The generation gate is
untouched: only the explicit "确认终稿" publish path writes project script
versions via ``/projects/{id}/scripts``; this module never does.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor

DraftKind = Literal["copy", "oral", "replica", "publishing"]
DRAFT_KINDS: tuple[str, ...] = ("copy", "oral", "replica", "publishing")

MAX_DRAFT_PAYLOAD_BYTES = 512_000
MAX_SAVED_SCRIPT_TEXT_CHARS = 100_000
MAX_SAVED_SCRIPTS_PER_USER = 50


def _utc_now_text() -> str:
    """Microsecond-precision timestamp; CURRENT_TIMESTAMP alone is
    second-granular and cannot break ties for uuid-keyed rows."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


class StudioDraftUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, object]
    script_confirmed: bool = False
    expected_revision: int | None = Field(default=None, ge=0)


class StudioDraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_kind: str
    payload: dict[str, object]
    script_confirmed: bool
    revision: int
    updated_at: str


class SavedScriptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=MAX_SAVED_SCRIPT_TEXT_CHARS)
    original: str | None = None
    version: int = Field(default=1, ge=1)
    ip_id: str | None = None
    source_project_id: str | None = None
    source_kind: Literal["viral", "project", "link", "upload"] | None = None


class SavedScriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    script_id: str
    title: str
    text: str
    original: str | None
    version: int
    ip_id: str | None
    source_project_id: str | None
    source_kind: str | None
    created_at: str
    updated_at: str


class SavedScriptListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SavedScriptResponse]


def _draft_not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "STUDIO_DRAFT_NOT_FOUND", "message": "暂无云端草稿。"},
    )


def _validate_draft_kind(kind: str) -> None:
    if kind not in DRAFT_KINDS:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "STUDIO_DRAFT_KIND_INVALID",
                "message": "不支持的工作区草稿类型。",
            },
        )


def load_studio_draft(
    conn: BusinessConnection,
    *,
    actor_id: str,
    kind: str,
) -> StudioDraftResponse:
    row = conn.execute(
        """
        SELECT * FROM studio_drafts
        WHERE user_id = %s AND draft_kind = %s
        """,
        (actor_id, kind),
    ).fetchone()
    if row is None:
        raise _draft_not_found()
    return _draft_response(row)


def save_studio_draft(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    kind: str,
    request: StudioDraftUpsertRequest,
) -> StudioDraftResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.draft_write",
        entity_type="studio_draft",
        entity_id=kind,
    )
    _validate_draft_kind(kind)
    payload_text = json.dumps(request.payload, ensure_ascii=False, sort_keys=True)
    if len(payload_text.encode("utf-8")) > MAX_DRAFT_PAYLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "STUDIO_DRAFT_PAYLOAD_TOO_LARGE",
                "message": "草稿内容过大，请精简后再保存。",
            },
        )
    confirmed_flag = 1 if request.script_confirmed else 0
    if kind == "publishing" and request.expected_revision is None:
        raise HTTPException(
            422,
            detail={
                "code": "STUDIO_DRAFT_REVISION_REQUIRED",
                "message": "请先加载发布草稿后再保存。",
            },
        )
    if request.expected_revision is not None:
        if request.expected_revision == 0:
            row = conn.execute(
                """INSERT INTO studio_drafts
                (id, user_id, draft_kind, payload, script_confirmed, revision)
                VALUES (%s, %s, %s, %s, %s, 1)
                ON CONFLICT (user_id, draft_kind) DO NOTHING RETURNING *""",
                (str(uuid4()), actor.id, kind, payload_text, confirmed_flag),
            ).fetchone()
        else:
            row = conn.execute(
                """UPDATE studio_drafts SET payload = %s, script_confirmed = %s,
                revision = revision + 1, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s AND draft_kind = %s AND revision = %s RETURNING *""",
                (payload_text, confirmed_flag, actor.id, kind, request.expected_revision),
            ).fetchone()
        if row is None:
            raise HTTPException(
                409,
                detail={
                    "code": "STUDIO_DRAFT_CONFLICT",
                    "message": "云端草稿已在其他窗口更新，请重新加载后再保存。",
                },
            )
        conn.commit()
        return _draft_response(row)
    conn.execute(
        """
        INSERT INTO studio_drafts (
            id, user_id, draft_kind, payload, script_confirmed, revision
        ) VALUES (%s, %s, %s, %s, %s, 1)
        ON CONFLICT (user_id, draft_kind) DO UPDATE SET
            payload = excluded.payload,
            script_confirmed = excluded.script_confirmed,
            revision = studio_drafts.revision + 1,
            updated_at = CURRENT_TIMESTAMP
        """,
        (str(uuid4()), actor.id, kind, payload_text, confirmed_flag),
    )
    row = conn.execute(
        "SELECT * FROM studio_drafts WHERE user_id = %s AND draft_kind = %s",
        (actor.id, kind),
    ).fetchone()
    if row is None:  # pragma: no cover - upsert always yields the row
        raise _draft_not_found()
    conn.commit()
    return _draft_response(row)


def delete_studio_draft(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    kind: str,
) -> None:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.draft_delete",
        entity_type="studio_draft",
        entity_id=kind,
    )
    _validate_draft_kind(kind)
    cursor = conn.execute(
        "DELETE FROM studio_drafts WHERE user_id = %s AND draft_kind = %s",
        (actor.id, kind),
    )
    if getattr(cursor, "rowcount", 0) == 0:
        raise _draft_not_found()
    conn.commit()


def list_saved_scripts(
    conn: BusinessConnection,
    *,
    actor_id: str,
) -> SavedScriptListResponse:
    rows = conn.execute(
        """
        SELECT * FROM studio_saved_scripts
        WHERE user_id = %s
        ORDER BY updated_at DESC, id DESC
        LIMIT %s
        """,
        (actor_id, MAX_SAVED_SCRIPTS_PER_USER),
    ).fetchall()
    return SavedScriptListResponse(items=[_saved_script_response(row) for row in rows])


def load_saved_script(
    conn: BusinessConnection,
    *,
    actor_id: str,
    script_id: str,
) -> SavedScriptResponse:
    row = conn.execute(
        "SELECT * FROM studio_saved_scripts WHERE user_id = %s AND script_id = %s",
        (actor_id, script_id),
    ).fetchone()
    if row is None:
        raise HTTPException(
            404,
            detail={
                "code": "STUDIO_SAVED_SCRIPT_NOT_FOUND",
                "message": "该保存文案不存在或已被删除。",
            },
        )
    return _saved_script_response(row)


def save_saved_script(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    request: SavedScriptRequest,
) -> SavedScriptResponse:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.saved_script_write",
        entity_type="studio_saved_script",
        entity_id=request.script_id,
    )
    if not request.text.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "STUDIO_SAVED_SCRIPT_TEXT_REQUIRED",
                "message": "文案内容不能为空。",
            },
        )
    now = _utc_now_text()
    conn.execute(
        """
        INSERT INTO studio_saved_scripts (
            id, user_id, script_id, title, text, original, version,
            ip_id, source_project_id, source_kind, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, script_id) DO UPDATE SET
            title = excluded.title,
            text = excluded.text,
            original = excluded.original,
            version = excluded.version,
            ip_id = excluded.ip_id,
            source_project_id = excluded.source_project_id,
            source_kind = excluded.source_kind,
            updated_at = excluded.updated_at
        """,
        (
            str(uuid4()),
            actor.id,
            request.script_id,
            request.title,
            request.text,
            request.original,
            request.version,
            request.ip_id,
            request.source_project_id,
            request.source_kind,
            now,
            now,
        ),
    )
    row = conn.execute(
        """
        SELECT * FROM studio_saved_scripts
        WHERE user_id = %s AND script_id = %s
        """,
        (actor.id, request.script_id),
    ).fetchone()
    if row is None:  # pragma: no cover - upsert always yields the row
        raise _draft_not_found()
    conn.commit()
    return _saved_script_response(row)


def delete_saved_script(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    script_id: str,
) -> None:
    require_not_auditor(
        conn,
        actor=actor,
        action="studio.saved_script_delete",
        entity_type="studio_saved_script",
        entity_id=script_id,
    )
    cursor = conn.execute(
        "DELETE FROM studio_saved_scripts WHERE user_id = %s AND script_id = %s",
        (actor.id, script_id),
    )
    if getattr(cursor, "rowcount", 0) == 0:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "STUDIO_SAVED_SCRIPT_NOT_FOUND",
                "message": "该保存文案不存在或已被删除。",
            },
        )
    conn.commit()


def _draft_response(row: sqlite3.Row) -> StudioDraftResponse:
    return StudioDraftResponse(
        draft_kind=str(row["draft_kind"]),
        payload=cast(dict[str, object], json.loads(str(row["payload"]))),
        script_confirmed=bool(int(row["script_confirmed"])),
        revision=int(row["revision"]),
        updated_at=str(row["updated_at"]),
    )


def _saved_script_response(row: sqlite3.Row) -> SavedScriptResponse:
    return SavedScriptResponse(
        script_id=str(row["script_id"]),
        title=str(row["title"]),
        text=str(row["text"]),
        original=None if row["original"] is None else str(row["original"]),
        version=int(row["version"]),
        ip_id=None if row["ip_id"] is None else str(row["ip_id"]),
        source_project_id=(
            None if row["source_project_id"] is None else str(row["source_project_id"])
        ),
        source_kind=None if row["source_kind"] is None else str(row["source_kind"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
