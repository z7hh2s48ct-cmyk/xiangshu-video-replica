from __future__ import annotations

from fastapi import HTTPException

from app.bootstrap import is_customer_production


def reject_legacy_sync_operation(*, replacement: str) -> None:
    """Prevent legacy long-running HTTP work at the customer-production boundary."""

    if not is_customer_production():
        return
    raise HTTPException(
        status_code=410,
        detail={
            "code": "ASYNC_TASK_REQUIRED",
            "message": "该旧入口已停用，请通过后台任务接口提交并恢复进度。",
            "replacement": replacement,
        },
    )
