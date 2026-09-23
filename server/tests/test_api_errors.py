"""Tests for the centralized HTTP exception factory (C1).

These lock in the behaviors that the per-module ``_http`` helpers provided
before they were consolidated, most importantly the ops result-code reporting
that five of the eight migrated route modules relied on.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api_errors import http_error
from app.ops_metrics import get_current_result_code, set_current_result_code


def test_builds_code_message_detail_envelope() -> None:
    exc = http_error(404, "CUSTOMER_NOT_FOUND", "customer does not exist")

    assert isinstance(exc, HTTPException)
    assert exc.status_code == 404
    assert exc.detail == {"code": "CUSTOMER_NOT_FOUND", "message": "customer does not exist"}


def test_extra_fields_merge_into_detail() -> None:
    exc = http_error(
        409,
        "SESSION_FENCED",
        "session was fenced",
        session_epoch=7,
        retry_after=30,
    )

    assert exc.detail == {
        "code": "SESSION_FENCED",
        "message": "session was fenced",
        "session_epoch": 7,
        "retry_after": 30,
    }


def test_reports_result_code_for_ops_metrics() -> None:
    """Regression guard: the pre-consolidation ``_http`` set the ops result code.

    Losing this silently blanks ``request.state.result_code`` for every error
    response raised through the factory, which is what the monitoring pipeline
    reads to label failures.
    """
    set_current_result_code("SENTINEL_UNSET")

    http_error(403, "DEVICE_NOT_TRUSTED", "device is not trusted")

    assert get_current_result_code() == "DEVICE_NOT_TRUSTED"


def test_reports_result_code_even_when_extra_fields_present() -> None:
    set_current_result_code("SENTINEL_UNSET")

    http_error(409, "SESSION_FENCED", "session was fenced", session_epoch=2)

    assert get_current_result_code() == "SESSION_FENCED"


@pytest.mark.parametrize("status", [399, 600, 0, -1])
def test_rejects_status_outside_error_range(status: int) -> None:
    with pytest.raises(ValueError, match="Invalid HTTP status"):
        http_error(status, "CODE", "message")


@pytest.mark.parametrize("code", ["", None])
def test_rejects_empty_code(code: object) -> None:
    with pytest.raises(ValueError, match="error code"):
        http_error(400, code, "message")  # type: ignore[arg-type]


@pytest.mark.parametrize("message", ["", None])
def test_rejects_empty_message(message: object) -> None:
    with pytest.raises(ValueError, match="message"):
        http_error(400, "CODE", message)  # type: ignore[arg-type]
