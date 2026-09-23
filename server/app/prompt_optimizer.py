"""Durable, optional H3 prompt editing; a task never creates a video batch."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import Field

from app.analysis import AnalysisProviderFailed
from app.auth import CurrentUser, Role
from app.db_portable import BusinessConnection
from app.h3_prompts import (
    FORMATTER_VERSION,
    RULES,
    GenerationContext,
    Issue,
    dialogue,
    digest,
    enforce_reference_video_exclusions,
    mode_rules,
    prompt_issues,
)
from app.prompt_context import attach_context_media, context_source_data, resolve_context
from app.storage import StorageAdapter

PROMPT_OPTIMIZE_SERVICE = "prompt_optimize"


class PromptOptimizeRequest(GenerationContext):
    idempotency_key: str = Field(min_length=1, max_length=128)
    editor_revision: int = Field(ge=0)
    prompt_text: str = Field(min_length=1, max_length=7000)

    def context(self) -> GenerationContext:
        return GenerationContext.model_validate(
            self.model_dump(exclude={"idempotency_key", "editor_revision", "prompt_text"})
        )


def task_result(row: Any) -> dict[str, Any]:
    request = json.loads(str(row["request_json"]))
    return {
        "task_id": str(row["id"]),
        "status": str(row["status"]),
        "mode": str(row["mode"]),
        "editor_revision": request["editor_revision"],
        "context_hash": request["context"]["context_hash"],
        "formatter_version": FORMATTER_VERSION,
        "result": json.loads(str(row["response_json"])) if row["response_json"] else None,
        "error_code": row["error_code"],
        "error_message": row["error_message_redacted"],
    }


def enqueue(
    conn: BusinessConnection, *, actor: CurrentUser, request: PromptOptimizeRequest
) -> dict[str, Any]:
    from app.permissions import require_not_auditor
    from app.usage_billing import accept_operation

    require_not_auditor(
        conn,
        actor=actor,
        action="prompt.optimize",
        entity_type="prompt_optimization",
        entity_id=actor.id,
    )
    if not request.prompt_text.strip() or not request.idempotency_key.strip():
        raise HTTPException(422, detail={"code": "PROMPT_TEXT_REQUIRED"})
    request_hash = digest(
        {
            **request.model_dump(exclude={"idempotency_key", "editor_revision"}),
            "formatter_version": FORMATTER_VERSION,
        }
    )
    existing = conn.execute(
        "SELECT * FROM prompt_optimization_receipts WHERE owner_user_id=%s AND idempotency_key=%s",
        (actor.id, request.idempotency_key),
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
        return task_result(existing)
    context = resolve_context(conn, actor=actor, request=request.context())
    snapshot = {
        "prompt_text": request.prompt_text,
        "editor_revision": request.editor_revision,
        "input_context": request.context().model_dump(),
        "context": context,
    }
    from app.h3_prompts import protected_dialogue

    sources = context_source_data(conn, context)
    script = sources.get("script_version_id")
    snapshot["protected_dialogue"] = (
        str(script["full_text"])
        if isinstance(script, dict)
        else protected_dialogue(request.prompt_text)
    )
    task_id = str(uuid4())
    cursor = conn.execute(
        """INSERT INTO prompt_optimization_receipts
        (id,owner_user_id,idempotency_key,mode,request_hash,status,request_json,lease_owner,lease_expires_at)
        VALUES (%s,%s,%s,%s,%s,'PENDING',%s,'',%s)
        ON CONFLICT (owner_user_id,idempotency_key) DO NOTHING""",
        (
            task_id,
            actor.id,
            request.idempotency_key,
            context["mode"],
            request_hash,
            json.dumps(snapshot),
            datetime.now(UTC).isoformat(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM prompt_optimization_receipts WHERE owner_user_id=%s AND idempotency_key=%s",
        (actor.id, request.idempotency_key),
    ).fetchone()
    if row["request_hash"] != request_hash:
        raise HTTPException(409, detail={"code": "IDEMPOTENCY_CONFLICT"})
    if cursor.rowcount == 1:
        accept_operation(
            conn, user_id=actor.id, service=PROMPT_OPTIMIZE_SERVICE, source_id=task_id, units=1
        )
    return task_result(row)


def validate_result(text: str, *, snapshot: dict[str, Any]) -> tuple[dict[str, Any], str]:
    context = snapshot["context"]
    value = json.loads(text)
    if (
        not isinstance(value, dict)
        or set(value) != {"prompt_text", "warnings"}
        or not isinstance(value["warnings"], list)
    ):
        raise ValueError("invalid optimizer envelope")
    warnings = [Issue.model_validate(item).model_dump() for item in value["warnings"]]
    prompt = value["prompt_text"]
    if prompt is None:
        return {
            "prompt_text": None,
            "warnings": warnings,
            "validation_status": "needs_input",
        }, "NEEDS_INPUT"
    if not isinstance(prompt, str):
        raise ValueError("invalid prompt text")
    if context["mode"] == "Ref2VA":
        prompt = enforce_reference_video_exclusions(prompt)
    issues = prompt_issues(
        prompt,
        mode=context["mode"],
        duration=context["duration_seconds"],
        labels=[a["label"] for a in context["generation_assets"]],
    )
    from app.h3_prompts import protected_dialogue

    protected = snapshot.get("protected_dialogue", protected_dialogue(snapshot["prompt_text"]))
    if protected is not None and "".join(str(protected).split()) != dialogue(prompt):
        issues.append(Issue(code="DIALOGUE_CHANGED", message="优化结果修改了原台词，请核对。"))
    if issues:
        return {
            "prompt_text": prompt,
            "warnings": warnings + [i.model_dump() for i in issues],
            "validation_status": "invalid",
        }, "FAILED"
    if warnings:
        return {
            "prompt_text": prompt,
            "warnings": warnings,
            "validation_status": "needs_input",
        }, "NEEDS_INPUT"
    return {
        "prompt_text": prompt,
        "warnings": warnings,
        "validation_status": "valid",
        "reference_bindings": context["generation_assets"],
    }, "SUCCEEDED"


def run_prompt_task(
    connection: Callable[[], AbstractContextManager[BusinessConnection]],
    *,
    worker_id: str,
    storage: StorageAdapter,
) -> bool:
    """Short transactions surround one provider request; a lost lease never resubmits."""
    from app.prompt_optimizer_routes import get_prompt_optimizer_provider
    from app.usage_billing import begin_source_attempt, complete_source_attempt, finish_source

    with connection() as conn:
        row = conn.execute(
            """UPDATE prompt_optimization_receipts SET status='RUNNING',lease_owner=%s,
            lease_expires_at=%s,updated_at=CURRENT_TIMESTAMP
            WHERE id=(SELECT id FROM prompt_optimization_receipts WHERE status='PENDING'
            ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1)
            AND status='PENDING' RETURNING *""",
            (worker_id, (datetime.now(UTC) + timedelta(minutes=10)).isoformat()),
        ).fetchone()
        if row is None:
            return False
        task_id = str(row["id"])
        snapshot = json.loads(str(row["request_json"]))
    sent = False
    received = False
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    try:
        with connection() as conn:
            user = conn.execute(
                "SELECT id,username,display_name,role FROM users WHERE id=%s",
                (row["owner_user_id"],),
            ).fetchone()
            if user is None:
                raise HTTPException(404, detail={"code": "USER_NOT_FOUND"})
            actor = CurrentUser(
                id=str(user["id"]),
                username=str(user["username"]),
                display_name=str(user["display_name"]),
                role=cast(Role, str(user["role"])),
            )
            current = resolve_context(
                conn,
                actor=actor,
                request=GenerationContext.model_validate(snapshot["input_context"]),
            )
            if current["context_hash"] != snapshot["context"]["context_hash"]:
                raise HTTPException(409, detail={"code": "PROMPT_CONTEXT_CHANGED"})
            if current["issues"]:
                result = {
                    "prompt_text": None,
                    "warnings": current["issues"],
                    "validation_status": "needs_input",
                }
                state = "NEEDS_INPUT"
                client = None
                media: list[dict[str, Any]] = []
                sources: dict[str, Any] = {}
            else:
                client = get_prompt_optimizer_provider(conn)
                media = attach_context_media(conn, actor=actor, context=current, storage=storage)
                sources = context_source_data(conn, current)
        if client is not None:
            with connection() as conn:
                cursor = conn.execute(
                    "UPDATE prompt_optimization_receipts SET provider_started_at=%s "
                    "WHERE id=%s AND status='RUNNING' AND lease_owner=%s",
                    (datetime.now(UTC).isoformat(), task_id, worker_id),
                )
                if cursor.rowcount != 1:
                    return True
                begin_source_attempt(conn, task_id)
            sent = True
            # Transport returns before semantic validation. A malformed answer still costs.
            raw_text, _ = client._complete(
                {
                    "model": client.model,
                    "temperature": 0,
                    "max_tokens": 10000,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {
                            "role": "system",
                            "content": (RULES / "optimizer.txt").read_text(encoding="utf-8")
                            + "\n"
                            + mode_rules(current["mode"]),
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {
                                            "prompt_text": snapshot["prompt_text"],
                                            "context": current,
                                            "sources": sources,
                                            "protected_text": snapshot.get("protected_dialogue"),
                                            "max_prompt_chars": 7000,
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                                *media,
                            ],
                        },
                    ],
                }
            )
            received = True
            result, state = validate_result(raw_text, snapshot=snapshot)
    except Exception as exc:
        if isinstance(exc, AnalysisProviderFailed) and exc.failure_phase == "response":
            received = sent
        state = "SUBMISSION_UNCERTAIN" if sent and not received else "FAILED"
        if isinstance(exc, AnalysisProviderFailed) and exc.http_status is not None:
            state = "FAILED"
        error_code = (
            "PROMPT_OPTIMIZE_SUBMISSION_UNCERTAIN"
            if state == "SUBMISSION_UNCERTAIN"
            else "PROMPT_OPTIMIZE_FAILED"
        )
        error_message = (
            "优化结果未知，原文已保留，请先核对任务。"
            if state == "SUBMISSION_UNCERTAIN"
            else "提示词优化未完成，原文已保留。"
        )
        if isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
            error_code = str(exc.detail.get("code", error_code))
            error_message = str(exc.detail.get("message", error_message))
    with connection() as conn:
        cursor = conn.execute(
            """UPDATE prompt_optimization_receipts SET status=%s,response_json=%s,
            error_code=%s,error_message_redacted=%s,
            completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
            WHERE id=%s AND status='RUNNING' AND lease_owner=%s""",
            (
                state,
                json.dumps(result) if result else None,
                error_code,
                error_message,
                task_id,
                worker_id,
            ),
        )
        if sent:
            complete_source_attempt(conn, task_id, usage=1 if received else None)
        if cursor.rowcount == 1:
            finish_source(
                conn,
                task_id,
                units=1 if state == "SUCCEEDED" else 0,
                succeeded=state == "SUCCEEDED",
            )
    return True
