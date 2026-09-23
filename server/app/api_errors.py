"""Centralized HTTP exception factory (C1).

Replaces the duplicated local ``_http(status, code, message)`` helpers that each
route module defined for itself with a single shared implementation building a
consistent FastAPI HTTPException shape:

    HTTPException(
        status_code=status,
        detail={"code": error_code, "message": human_message},
        **extra_kwargs,
    )

Key behaviors:
• Reports the error code to ops metrics, matching what the replaced helpers did
• Preserves optional extra dict fields
• Enforces status in [400, 599], non-empty code/message
• Returns typed HTTPException for static analysis
"""

from __future__ import annotations

from fastapi import HTTPException

from app.ops_metrics import set_current_result_code


def http_error(status: int, code: str, message: str, **extra: object) -> HTTPException:
    """Construct a standardized HTTPException with code+message detail envelope.

    Also records ``code`` as the current ops result code so error responses stay
    labelled in the monitoring pipeline.

    Args:
        status: HTTP status code (must be in [400, 599])
        code: Machine-readable error code (e.g., "CUSTOMER_NOT_FOUND")
        message: Human-readable description
        **extra: Additional fields merged into detail dict (deprecated pattern;
                 prefer adding codes to central registry instead)

    Returns:
        HTTPException ready to raise

    Raises:
        ValueError: If status outside valid range or required fields empty
    """
    if not (400 <= status <= 599):
        raise ValueError(f"Invalid HTTP status {status}; must be in [400, 599]")
    if not code or not isinstance(code, str):
        raise ValueError("error code must be a non-empty string")
    if not message or not isinstance(message, str):
        raise ValueError("message must be a non-empty string")

    set_current_result_code(code)

    detail: dict[str, object] = {"code": code, "message": message}
    if extra:
        detail.update(extra)

    return HTTPException(status_code=status, detail=detail)
