from __future__ import annotations

from datetime import UTC, datetime

USABLE_SOURCE_QUALITY_STATUSES = frozenset({"PASSED", "IMPORTED"})
INTERNAL_SHORT_VIDEO_SCOPES = frozenset({"internal-short-video", "内部短视频"})


def effective_identity_state_values(
    *,
    status: object,
    authorization_status: object,
    authorization_expires_at: object,
    source_quality_status: object,
) -> tuple[str, str]:
    stored_status = str(status)
    stored_authorization = str(authorization_status)
    if stored_status == "ARCHIVED":
        return stored_authorization, "ARCHIVED"
    if stored_status == "REVOKED" or stored_authorization == "REVOKED":
        return "REVOKED", "REVOKED"
    if authorization_is_expired(authorization_expires_at):
        return "EXPIRED", "EXPIRED"
    if (
        stored_authorization == "AUTHORIZED"
        and str(source_quality_status) in USABLE_SOURCE_QUALITY_STATUSES
    ):
        return "AUTHORIZED", "ACTIVE"
    return stored_authorization, "DRAFT"


def identity_values_are_current(
    *,
    status: object,
    authorization_status: object,
    authorization_expires_at: object,
    source_quality_status: object,
) -> bool:
    effective_authorization, effective_status = effective_identity_state_values(
        status=status,
        authorization_status=authorization_status,
        authorization_expires_at=authorization_expires_at,
        source_quality_status=source_quality_status,
    )
    return effective_authorization == "AUTHORIZED" and effective_status == "ACTIVE"


def authorization_is_expired(value: object) -> bool:
    if value is None:
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC) <= datetime.now(UTC)


def scope_allows_project(scope: object, *, project_id: str) -> bool:
    if not isinstance(scope, (list, tuple, set, frozenset)):
        return False
    values = {str(value).strip() for value in scope if str(value).strip()}
    return project_id in values or bool(values & INTERNAL_SHORT_VIDEO_SCOPES)
