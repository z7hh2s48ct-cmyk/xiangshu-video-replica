"""Create/query optional H3 editing jobs; provider calls belong to the worker."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.analysis import ApilioGemini
from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.db_portable import BusinessConnection
from app.prompt_optimizer import PromptOptimizeRequest, enqueue, task_result
from app.settings import SettingsRepository, SettingsUnavailableError

router = APIRouter(prefix="/api/prompt-optimizations", tags=["prompts"])


def get_prompt_optimizer_provider(conn: BusinessConnection) -> ApilioGemini:
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError as exc:
        raise HTTPException(
            503,
            detail={"code": "PROMPT_OPTIMIZER_UNAVAILABLE", "message": "请先配置提示词优化服务。"},
        ) from exc
    key = config.get("analysis_api_key") or config.get("api_key")
    if not key:
        raise HTTPException(
            503,
            detail={"code": "PROMPT_OPTIMIZER_UNAVAILABLE", "message": "请先配置提示词优化服务。"},
        )
    return ApilioGemini(api_key=key)


@router.post("", status_code=202)
def create_prompt_optimization(request: PromptOptimizeRequest, db: BusinessDbDep) -> dict[str, Any]:
    with db.write() as (conn, actor):
        return enqueue(conn, actor=actor, request=request)


@router.get("/{task_id}")
def read_prompt_optimization(
    task_id: str, conn: Database, actor: AuthenticatedUser
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM prompt_optimization_receipts WHERE id=%s AND owner_user_id=%s",
        (task_id, actor.id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, detail={"code": "PROMPT_OPTIMIZATION_NOT_FOUND"})
    return task_result(row)
