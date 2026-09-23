"""Admin reconcile for first-frame tasks stuck in SUBMISSION_UNCERTAIN.

同步两段式：事务内校验状态并解析回执 → 事务外凭回执询问供应商真实结果 →
事务内围栏改状态并写审计。视频线（generation.py）的 reconcile 是异步操作
队列；首帧任务一次轮询即可裁决，同步端点足够。重复提交天然幂等：第二次
调用会撞上 409 IMAGE_TASK_NOT_UNCERTAIN / IMAGE_TASK_STATE_CHANGED。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from app.admin_auth_routes import AdminActor, AdminWriter
from app.api_errors import http_error as _http
from app.auth import CurrentUser
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.first_frame_routes import get_image_provider
from app.first_frames import ApilioImageProvider
from app.image_tasks import (
    apply_first_frame_reconcile,
    first_frame_reconcile_decision,
    prepare_first_frame_reconcile,
)

router = APIRouter(prefix="/api/control", tags=["admin-first-frame"])


def _admin_to_current_user(actor: AdminActor) -> CurrentUser:
    return CurrentUser(
        id=actor.user_id,
        username=actor.username,
        display_name=actor.display_name,
        role="admin",
    )


@router.post("/first-frame-tasks/{task_id}/reconcile")
def reconcile_first_frame_task(task_id: str, actor: AdminWriter) -> dict[str, object]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        plan = prepare_first_frame_reconcile(conn, task_id=task_id)
        provider = get_image_provider(conn)
    raw_decision: Literal["RESUME", "FAIL", "RETRY"]
    detail_code: str | None
    if not isinstance(provider, ApilioImageProvider):
        # The receipt belongs to an Apilio account; a different (or fake)
        # provider configuration can never poll it — the config has changed.
        raw_decision, detail_code = "FAIL", "IMAGE_TASK_PROVIDER_CHANGED"
    else:
        raw_decision, detail_code = first_frame_reconcile_decision(plan, provider)
    if raw_decision == "RETRY":
        if detail_code == "IMAGE_TASK_RECONCILE_PROVIDER_UNAVAILABLE":
            raise _http(
                503,
                "IMAGE_TASK_RECONCILE_PROVIDER_UNAVAILABLE",
                "图像服务暂时不可用，请稍后重试。",
            )
        raise _http(
            409,
            "IMAGE_TASK_RECONCILE_INCONCLUSIVE",
            "图像服务返回无法解读，请稍后重试或联系排查。",
        )
    decision: Literal["RESUME", "FAIL"] = raw_decision
    with pg_transaction() as raw:
        result = apply_first_frame_reconcile(
            BusinessConnection.postgres(raw),
            task_id=task_id,
            decision=decision,
            detail_code=detail_code,
            actor=_admin_to_current_user(actor),
        )
    return {"task_id": task_id, "result": result, "detail_code": detail_code}
