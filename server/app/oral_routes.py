"""Oral digital-human routes (/api/oral/*, C1).

The dependency ``get_oral_vendor`` is the single seam tests override; routes
never surface vendor identifiers — row serialization strips ``vendor_*``
fields so the customer UI cannot leak the upstream provider name.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.customer_fence import BusinessDbDep
from app.hifly import (
    HiflyClient,
    HiflyError,
    HiflySubmissionUncertain,
    hifly_client_from_settings,
)
from app.internal_billing import (
    BillingInvariantError,
    InsufficientCreditsError,
    reconcile_oral_billing_by_evidence,
)
from app.oral import (
    ORAL_CONSENT_TEXT_VERSION,
    OralConflictError,
    OralDomainError,
    OralResourceInUseError,
    OralResourceNotFoundError,
    OralTaskNotFoundError,
    cancel_oral_task,
    confirm_voice_clone,
    create_oral_consent,
    create_oral_task,
    delete_avatar_clone,
    delete_voice_clone,
    list_avatars,
    list_oral_consents,
    list_oral_tasks,
    list_voices,
    oral_price_quote,
    oral_task_available_actions,
    oral_terminal_billing_states,
    read_avatar_clone,
    read_oral_task,
    read_voice_clone,
    start_avatar_clone,
    start_voice_clone,
)
from app.oral_worker import request_oral_archive_retry
from app.permissions import require_not_auditor, require_role, write_audit

router = APIRouter(prefix="/api/oral")


def get_oral_vendor(conn: Database) -> HiflyClient:
    return hifly_client_from_settings(conn)


OralVendor = Annotated[HiflyClient, Depends(get_oral_vendor)]


class OralTaskResponse(BaseModel):
    id: str
    status: Literal[
        "QUEUED",
        "SUBMITTING",
        "RUNNING",
        "ARCHIVING",
        "SUBMISSION_UNCERTAIN",
        "ARCHIVE_FAILED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
    ]
    title: str
    mode: Literal["TTS", "AUDIO"]
    identity_id: str
    avatar_id: str
    voice_id: str | None
    script_text: str | None
    audio_asset_id: str | None
    status_message: str | None = None
    error_message: str | None = None
    result_asset_id: str | None
    duration_sec: float | None
    estimated_cost_fen: int
    billing_status: str | None = None
    available_actions: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class OralTaskPageResponse(BaseModel):
    items: list[OralTaskResponse]
    total: int
    limit: int
    offset: int


class OralError(HTTPException):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(status_code=status_code, detail={"code": code, "message": message})


def _domain_guard(exc: OralDomainError) -> HTTPException:
    if isinstance(exc, OralTaskNotFoundError):
        return OralError("ORAL_TASK_NOT_FOUND", str(exc), status_code=404)
    if isinstance(exc, OralResourceNotFoundError):
        return OralError("ORAL_RESOURCE_NOT_FOUND", str(exc), status_code=404)
    if isinstance(exc, OralResourceInUseError):
        return OralError("ORAL_RESOURCE_IN_USE", str(exc), status_code=409)
    if isinstance(exc, OralConflictError):
        return OralError("ORAL_IDEMPOTENCY_CONFLICT", str(exc), status_code=409)
    return OralError("ORAL_REQUEST_INVALID", str(exc))


def _vendor_guard(exc: HiflyError) -> HTTPException:
    if isinstance(exc, HiflySubmissionUncertain):
        return OralError("ORAL_SUBMISSION_UNCERTAIN", str(exc), status_code=503)
    return OralError("ORAL_VENDOR_UNAVAILABLE", str(exc), status_code=503)


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Customer-facing projection: opaque task ids only, never vendor fields."""
    return {
        key: value
        for key, value in row.items()
        if not key.startswith("vendor_")
        and key not in {"idempotency_key", "request_hash", "subtitle_json"}
    }


_TERMINAL_BILLING_LABELS = {"SETTLE": "SETTLED", "RELEASE": "RELEASED", "FREE": "FREE"}


def _serialize_task(row: dict[str, Any], terminals: dict[str, str]) -> dict[str, Any]:
    """Task projection with retry hints and the wallet truth of the current round.

    A task whose billing round has no terminal transaction still holds its
    reservation (open while active, frozen while submission-uncertain), so it
    reports ``RESERVED``.
    """
    data = _serialize(row)
    data["available_actions"] = oral_task_available_actions(row)
    if row.get("billing_round") is not None:
        data["billing_status"] = _TERMINAL_BILLING_LABELS.get(
            terminals.get(str(row["id"]), ""), "RESERVED"
        )
    return data


def _serialize_tasks_for_owner(conn: Database, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    terminals = oral_terminal_billing_states(conn, owner_user_id=str(rows[0]["owner_user_id"]))
    return [_serialize_task(row, terminals) for row in rows]


@router.get("/price")
def read_oral_price(conn: Database) -> dict[str, int | float]:
    return oral_price_quote(conn)


# ---------------------------------------------------------------------------
# Clone consent
# ---------------------------------------------------------------------------


class OralConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str = Field(min_length=1, max_length=128)
    source_asset_id: str = Field(min_length=1, max_length=128)
    purpose: Literal["AVATAR", "VOICE", "AVATAR_CLONE", "VOICE_CLONE"]


class OralBudgetRequest(BaseModel):
    script_text: str | None = Field(default=None, max_length=10000)
    audio_asset_id: str | None = Field(default=None, max_length=128)


@router.post("/quote")
def read_oral_budget(
    payload: OralBudgetRequest, conn: Database, actor: AuthenticatedUser
) -> dict[str, int | float]:
    from app.billing_catalog import oral_budget_units, retail_snapshot

    if payload.audio_asset_id:
        asset = conn.execute(
            "SELECT 1 FROM assets WHERE id=%s AND created_by_user_id=%s",
            (payload.audio_asset_id, actor.id),
        ).fetchone()
        if asset is None:
            raise OralError("ORAL_AUDIO_NOT_FOUND", "口播音频不存在", 404)
    units = oral_budget_units(
        conn, script_text=payload.script_text, audio_asset_id=payload.audio_asset_id
    )
    quote = retail_snapshot(conn, "oral", units)
    if payload.audio_asset_id and units == 0 and quote["enabled"]:
        raise OralError("BILLABLE_DURATION_REQUIRED", "缺少有效音频时长，请重新上传。")
    return {
        **oral_price_quote(conn),
        "budget_seconds": float(units),
        "estimated_credits": int(str(quote["credits"])),
    }


@router.post("/consents", status_code=status.HTTP_201_CREATED)
def create_clone_consent(
    request: OralConsentRequest,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        try:
            return create_oral_consent(
                conn,
                actor=actor,
                identity_id=request.identity_id,
                source_asset_id=request.source_asset_id,
                purpose=request.purpose,
                consent_text_version=ORAL_CONSENT_TEXT_VERSION,
            )
        except OralDomainError as exc:
            raise _domain_guard(exc) from exc


@router.get("/consents")
def read_clone_consents(
    identity_id: Annotated[str, Query(min_length=1, max_length=128)],
    conn: Database,
    actor: AuthenticatedUser,
) -> list[dict[str, Any]]:
    try:
        return list_oral_consents(conn, actor=actor, identity_id=identity_id)
    except OralDomainError as exc:
        raise _domain_guard(exc) from exc


# ---------------------------------------------------------------------------
# Avatars
# ---------------------------------------------------------------------------


@router.get("/avatars")
def list_oral_avatars(
    identity_id: Annotated[str, Query(min_length=1, max_length=128)],
    conn: Database,
    actor: AuthenticatedUser,
) -> list[dict[str, Any]]:
    return [_serialize(row) for row in list_avatars(conn, actor=actor, identity_id=identity_id)]


class AvatarCloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=60)
    source_asset_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["VIDEO"]
    consent_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.post("/avatars", status_code=status.HTTP_202_ACCEPTED)
def create_avatar_clone(
    request: AvatarCloneRequest,
    db: BusinessDbDep,
) -> dict[str, Any]:
    domain_error: OralDomainError | None = None
    result = None
    with db.write() as (conn, actor):
        try:
            result = start_avatar_clone(
                conn,
                actor=actor,
                identity_id=request.identity_id,
                title=request.title,
                source_asset_id=request.source_asset_id,
                source_kind=request.source_kind,
                consent_id=request.consent_id,
                idempotency_key=request.idempotency_key,
            )
        except OralDomainError as exc:
            domain_error = exc
    if domain_error is not None:
        raise _domain_guard(domain_error) from domain_error
    assert result is not None
    return {
        "id": result.task_id,
        "status": result.status,
        "submission_state": result.submission_state,
        "replayed": result.replayed,
    }


@router.post("/avatars/{avatar_id}/refresh")
def refresh_avatar(
    avatar_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> dict[str, Any]:
    try:
        row = read_avatar_clone(conn, avatar_id=avatar_id, actor=actor)
    except OralDomainError as exc:
        raise _domain_guard(exc) from exc
    return _serialize(row)


# ---------------------------------------------------------------------------
# Voices
# ---------------------------------------------------------------------------


@router.get("/voices")
def list_oral_voices(
    identity_id: Annotated[str, Query(min_length=1, max_length=128)],
    conn: Database,
    actor: AuthenticatedUser,
) -> list[dict[str, Any]]:
    return [_serialize(row) for row in list_voices(conn, actor=actor, identity_id=identity_id)]


class VoiceCloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=60)
    source_asset_id: str = Field(min_length=1, max_length=128)
    consent_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.post("/voices", status_code=status.HTTP_202_ACCEPTED)
def create_voice_clone(
    request: VoiceCloneRequest,
    db: BusinessDbDep,
) -> dict[str, Any]:
    domain_error: OralDomainError | None = None
    result = None
    with db.write() as (conn, actor):
        try:
            result = start_voice_clone(
                conn,
                actor=actor,
                identity_id=request.identity_id,
                title=request.title,
                source_asset_id=request.source_asset_id,
                consent_id=request.consent_id,
                idempotency_key=request.idempotency_key,
            )
        except OralDomainError as exc:
            domain_error = exc
    if domain_error is not None:
        raise _domain_guard(domain_error) from domain_error
    assert result is not None
    return {
        "id": result.task_id,
        "status": result.status,
        "submission_state": result.submission_state,
        "replayed": result.replayed,
    }


@router.post("/voices/{voice_id}/refresh")
def refresh_voice(
    voice_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> dict[str, Any]:
    try:
        row = read_voice_clone(conn, voice_id=voice_id, actor=actor)
    except OralDomainError as exc:
        raise _domain_guard(exc) from exc
    return _serialize(row)


@router.post("/voices/{voice_id}/confirm")
def confirm_voice(
    voice_id: str,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        try:
            return _serialize(confirm_voice_clone(conn, voice_id=voice_id, actor=actor))
        except OralDomainError as exc:
            raise _domain_guard(exc) from exc


@router.delete("/avatars/{avatar_id}")
def delete_avatar(
    avatar_id: str,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        try:
            return delete_avatar_clone(conn, avatar_id=avatar_id, actor=actor)
        except OralDomainError as exc:
            raise _domain_guard(exc) from exc


@router.delete("/voices/{voice_id}")
def delete_voice(
    voice_id: str,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        try:
            return delete_voice_clone(conn, voice_id=voice_id, actor=actor)
        except OralDomainError as exc:
            raise _domain_guard(exc) from exc


# ---------------------------------------------------------------------------
# Oral tasks
# ---------------------------------------------------------------------------


class OralTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_id: str = Field(min_length=1, max_length=128)
    avatar_id: str = Field(min_length=1, max_length=128)
    voice_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["TTS", "AUDIO"]
    title: str = Field(min_length=1, max_length=120)
    script_text: str | None = Field(default=None, max_length=10_000)
    audio_asset_id: str | None = Field(default=None, min_length=1, max_length=128)
    subtitle: dict[str, Any] | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)


class OralBillingReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reconciliation_operation_id: str = Field(min_length=8, max_length=128)
    provider_outcome: Literal["SUCCEEDED", "FAILED", "CANCELLED", "NOT_FOUND"]
    provider_charge_state: Literal["CHARGED", "NOT_CHARGED"]
    resolution: Literal["SETTLE", "RELEASE"]
    evidence_asset_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=4, max_length=500)


@router.post("/tasks", status_code=status.HTTP_202_ACCEPTED)
def create_oral_generation_task(
    request: OralTaskRequest,
    db: BusinessDbDep,
) -> dict[str, Any]:
    try:
        with db.write() as (conn, actor):
            result = create_oral_task(
                conn,
                actor=actor,
                identity_id=request.identity_id,
                avatar_id=request.avatar_id,
                voice_id=request.voice_id,
                mode=request.mode,
                title=request.title,
                script_text=request.script_text,
                audio_asset_id=request.audio_asset_id,
                subtitle=request.subtitle,
                idempotency_key=request.idempotency_key,
            )
    except OralDomainError as exc:
        raise _domain_guard(exc) from exc
    except InsufficientCreditsError as exc:
        raise OralError(
            "INSUFFICIENT_CREDITS",
            "可用次数不足，请先充值。",
            status_code=402,
        ) from exc
    return {
        "id": result.task_id,
        "status": result.status,
        "estimated_cost_fen": result.estimated_cost_fen,
        "replayed": result.replayed,
        "submission_state": result.submission_state,
    }


@router.get("/tasks", response_model=OralTaskPageResponse)
def list_oral_generation_tasks(
    conn: Database,
    actor: AuthenticatedUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows, total = list_oral_tasks(
        conn,
        actor=actor,
        limit=limit,
        offset=offset,
    )
    return {
        "items": _serialize_tasks_for_owner(conn, rows),
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get(
    "/tasks/{task_id}",
    response_model=OralTaskResponse,
    responses={404: {"description": "口播任务不存在或不属于当前账号"}},
)
def read_oral_generation_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> dict[str, Any]:
    try:
        row = read_oral_task(conn, task_id=task_id, actor=actor)
    except OralDomainError as exc:
        raise _domain_guard(exc) from exc
    return _serialize_tasks_for_owner(conn, [row])[0]


@router.post("/tasks/{task_id}/refresh")
def refresh_oral_generation_task(
    task_id: str,
    conn: Database,
    actor: AuthenticatedUser,
) -> dict[str, Any]:
    try:
        row = read_oral_task(conn, task_id=task_id, actor=actor)
    except OralDomainError as exc:
        raise _domain_guard(exc) from exc
    return _serialize_tasks_for_owner(conn, [row])[0]


@router.post("/tasks/{task_id}/archive-retry")
def retry_oral_archive(
    task_id: str,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        require_not_auditor(
            conn,
            actor=actor,
            action="oral.task.archive_retry",
            entity_type="oral_task",
            entity_id=task_id,
        )
        try:
            row = request_oral_archive_retry(conn, task_id=task_id, owner_user_id=actor.id)
        except ValueError as exc:
            raise OralError(
                "ORAL_ARCHIVE_RETRY_NOT_ALLOWED",
                "只有保留了成片地址的归档失败任务可重试归档。",
                status_code=409,
            ) from exc
        return _serialize_tasks_for_owner(conn, [row])[0]


@router.post("/tasks/{task_id}/cancel")
def cancel_oral_generation_task(
    task_id: str,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        try:
            row = cancel_oral_task(conn, task_id=task_id, actor=actor)
        except OralDomainError as exc:
            raise _domain_guard(exc) from exc
        return _serialize_tasks_for_owner(conn, [row])[0]


@router.post("/tasks/{task_id}/billing-reconcile")
def reconcile_oral_generation_billing(
    task_id: str,
    request: OralBillingReconcileRequest,
    db: BusinessDbDep,
) -> dict[str, Any]:
    with db.write() as (conn, actor):
        require_role(
            conn,
            actor=actor,
            allowed_roles={"admin"},
            action="oral.billing_reconcile",
            entity_type="oral_task",
            entity_id=task_id,
        )
        evidence = conn.execute(
            """
            SELECT id, sha256 FROM assets
            WHERE id = %s AND created_by_user_id = %s
            """,
            (request.evidence_asset_id, actor.id),
        ).fetchone()
        if evidence is None:
            raise OralError(
                "ORAL_BILLING_RECONCILE_CONFLICT",
                "口播账务状态与本次人工对账不一致。",
                status_code=409,
            )
        try:
            result = reconcile_oral_billing_by_evidence(
                conn,
                oral_task_id=task_id,
                reconciliation_operation_id=request.reconciliation_operation_id,
                provider_outcome=request.provider_outcome,
                provider_charge_state=request.provider_charge_state,
                resolution=request.resolution,
                reason=request.reason,
                evidence_asset_id=str(evidence["id"]),
                evidence_sha256=str(evidence["sha256"]),
            )
        except BillingInvariantError as exc:
            raise OralError(
                "ORAL_BILLING_RECONCILE_CONFLICT",
                "口播账务状态与本次人工对账不一致。",
                status_code=409,
            ) from exc
        write_audit(
            conn,
            actor=actor,
            action="oral.billing.reconcile",
            entity_type="oral_task",
            entity_id=task_id,
            metadata={
                "reconciliation_operation_id": request.reconciliation_operation_id,
                "provider_outcome": request.provider_outcome,
                "provider_charge_state": request.provider_charge_state,
                "resolution": request.resolution,
                "evidence_asset_id": str(evidence["id"]),
                "evidence_sha256": str(evidence["sha256"]),
                "reason": request.reason,
            },
            commit=False,
        )
    return {
        "task_id": result.task_id,
        "billing_round": result.billing_round,
        "transaction_type": result.transaction_type,
    }
