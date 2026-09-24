from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import HTTPException

from app.analysis_routes import require_async_analysis_route
from app.first_frame_routes import require_async_first_frame_route
from app.simple_character_routes import (
    require_async_character_regeneration_route,
    require_async_global_character_route,
    require_async_project_character_route,
)


@pytest.mark.parametrize(
    ("guard", "args", "replacement"),
    [
        (
            require_async_analysis_route,
            ("project-1",),
            "/api/projects/project-1/analysis-tasks",
        ),
        (
            require_async_first_frame_route,
            ("project-1",),
            "/api/projects/project-1/first-frame-tasks",
        ),
        (
            require_async_global_character_route,
            (),
            "/api/simple-characters/tasks/generate",
        ),
        (
            require_async_project_character_route,
            ("project-1",),
            "/api/simple-characters/tasks/project-1/generate",
        ),
        (
            require_async_character_regeneration_route,
            ("identity-1",),
            "/api/simple-characters/identities/identity-1/regenerate-contact-sheet-task",
        ),
    ],
)
def test_customer_production_rejects_legacy_long_running_routes(
    monkeypatch: pytest.MonkeyPatch,
    guard: Callable[..., None],
    args: tuple[str, ...],
    replacement: str,
) -> None:
    monkeypatch.setenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", "true")

    with pytest.raises(HTTPException) as exc_info:
        guard(*args)

    assert exc_info.value.status_code == 410
    assert exc_info.value.detail == {
        "code": "ASYNC_TASK_REQUIRED",
        "message": "该旧入口已停用，请通过后台任务接口提交并恢复进度。",
        "replacement": replacement,
    }


def test_internal_compatibility_route_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VIDEO_REPLICA_CUSTOMER_PRODUCTION", raising=False)

    require_async_analysis_route("project-1")
