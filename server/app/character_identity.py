from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
import struct
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import content_store
from app.analysis import (
    APILIO_DEFAULT_BASE_URL,
    APILIO_GEMINI_MODEL,
    AnalysisProviderFailed,
    ApilioChatTransport,
    UrllibApilioChatTransport,
)
from app.auth import CurrentUser
from app.character_contracts import (
    CharacterPersona,
    CharacterVersion,
    PersonIdentity,
    RequiredCharacterViewType,
)
from app.character_policy import (
    USABLE_SOURCE_QUALITY_STATUSES,
    authorization_is_expired,
    effective_identity_state_values,
)
from app.db_portable import BusinessConnection
from app.permissions import require_role, write_audit
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    UploadedObjectSizeMismatch,
    read_uploaded_object,
    require_storage_match,
    storage_object_ref_from_uri,
    store_verified_upload,
    verified_upload_object_key,
)

logger = logging.getLogger(__name__)

IdentityAssetPurpose = Literal["authorization", "source"]
CharacterAssetPurpose = Literal["generated", "approved"]

REQUIRED_CHARACTER_VIEW_TYPES: tuple[RequiredCharacterViewType, ...] = (
    "FRONT_FACE",
    "FRONT_HALF",
    "FRONT_FULL",
    "LEFT_45",
    "LEFT_SIDE",
)
CHARACTER_TEMPLATE_VERSION = "character-assets-v1"
CHARACTER_TEMPLATE = (
    "Keep the authorized person's identity stable while applying the frozen persona, "
    "costume, background and requested view type. Return one realistic reference image."
)
CHARACTER_TEMPLATE_HASH = hashlib.sha256(CHARACTER_TEMPLATE.encode()).hexdigest()

ALLOWED_SOURCE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png"}
ALLOWED_AUTHORIZATION_TYPES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}
MAX_SOURCE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_AUTHORIZATION_BYTES = 20 * 1024 * 1024
MIN_SOURCE_IMAGE_WIDTH = 720
MIN_SOURCE_IMAGE_HEIGHT = 720
MIN_SOURCE_SHARPNESS_SCORE = 0.6
UPLOAD_INTENT_EXPIRES_IN = timedelta(minutes=15)
SAFE_KEY_SEGMENT = re.compile(r"^[A-Za-z0-9._:-]+$")


class SourceImageInspection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_count: int = Field(ge=0)
    face_count: int = Field(ge=0)
    face_visible: bool
    sharpness_score: float = Field(ge=0, le=1)
    occlusion_detected: bool
    watermark_detected: bool
    notes: list[str]
    provider: str
    model: str


class SourceImageQualityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    width: int
    height: int
    person_count: int
    face_count: int
    face_visible: bool
    sharpness_score: float
    occlusion_detected: bool
    watermark_detected: bool
    issue_codes: list[str]
    notes: list[str]
    provider: str
    model: str


class SourceImageInspector(Protocol):
    def inspect(
        self,
        content: bytes,
        *,
        content_type: str,
    ) -> SourceImageInspection: ...


class FakeSourceImageInspector:
    """Deterministic local-only inspector for an explicitly isolated Gate 1 run."""

    def inspect(
        self,
        content: bytes,
        *,
        content_type: str,
    ) -> SourceImageInspection:
        if not content or content_type not in ALLOWED_SOURCE_TYPES:
            raise SourceImageInspectorFailed("fake source image input is invalid")
        return SourceImageInspection(
            person_count=1,
            face_count=1,
            face_visible=True,
            sharpness_score=0.95,
            occlusion_detected=False,
            watermark_detected=False,
            notes=["Gate 1 内置模拟检查"],
            provider="fake-source-inspector",
            model="fake-source-inspector-v1",
        )


class SourceImageInspectorFailed(RuntimeError):
    pass


class ApilioSourceImageInspector:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = APILIO_DEFAULT_BASE_URL,
        model: str = APILIO_GEMINI_MODEL,
        transport: ApilioChatTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport or UrllibApilioChatTransport()

    def inspect(
        self,
        content: bytes,
        *,
        content_type: str,
    ) -> SourceImageInspection:
        data_url = f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": source_image_inspection_instruction()},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        }
        try:
            raw_body, _ = self.transport.post(
                f"{self.base_url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                body=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(),
            )
            response = json.loads(raw_body.decode("utf-8"))
            raw_content = response["choices"][0]["message"]["content"]
            inspection_payload = json.loads(raw_content)
            if not isinstance(inspection_payload, dict):
                raise TypeError("source inspection response must be an object")
            inspection_payload["provider"] = "apilio_gemini"
            inspection_payload["model"] = self.model
            return SourceImageInspection.model_validate(inspection_payload)
        except AnalysisProviderFailed as exc:
            raise SourceImageInspectorFailed("source image inspector request failed") from exc
        except (
            IndexError,
            KeyError,
            TypeError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
            logger.warning("Source image inspector returned an invalid response")
            raise SourceImageInspectorFailed("source image inspector response was invalid") from exc


class CreatedIdentityUploadIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    identity_id: str
    purpose: IdentityAssetPurpose
    storage_key: str
    method: str
    url: str
    headers: dict[str, str]
    expires_at: str


class CompletedSourceImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity: PersonIdentity
    asset_id: str
    sha256: str
    size_bytes: int
    content_type: str
    quality: SourceImageQualityResult


def source_image_inspection_instruction() -> str:
    return (
        "Inspect this authorized portrait source image. Return JSON only with exactly these "
        "fields: person_count (integer), face_count (integer), face_visible (boolean), "
        "sharpness_score (number 0 to 1), occlusion_detected (boolean), "
        "watermark_detected (boolean), notes (array of short Chinese strings). "
        "Count every visible person. Mark face_visible false if the main face is cropped, "
        "turned away, too small, or too dark."
    )


def create_person_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    display_name: str,
    owner_user_id: str | None,
    authorization_scope: list[str],
    authorization_expires_at: datetime | None,
) -> PersonIdentity:
    require_character_admin(
        conn,
        actor=actor,
        action="person_identity.create",
        entity_type="person_identity",
        entity_id="new",
    )
    clean_name = required_text(display_name, "IDENTITY_NAME_REQUIRED", "人物显示名不能为空。")
    clean_scope = normalize_string_list(authorization_scope)
    if not clean_scope:
        raise character_error(
            422,
            "IDENTITY_AUTHORIZATION_SCOPE_REQUIRED",
            "至少填写一个肖像授权使用范围。",
        )
    effective_owner = owner_user_id or actor.id
    ensure_user_exists(conn, effective_owner)
    identity_id = str(uuid4())
    with conn:
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                authorization_scope, authorization_expires_at,
                source_quality_status, status, created_by
            )
            VALUES (%s, %s, %s, 'PENDING', %s, %s, 'PENDING', 'DRAFT', %s)
            """,
            (
                identity_id,
                effective_owner,
                clean_name,
                encode_json(clean_scope),
                encode_datetime(authorization_expires_at),
                actor.id,
            ),
        )
    write_audit(
        conn,
        actor=actor,
        action="person_identity.create",
        entity_type="person_identity",
        entity_id=identity_id,
        metadata={"owner_user_id": effective_owner},
    )
    return get_person_identity(conn, actor=actor, identity_id=identity_id)


def list_person_identities(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
) -> list[PersonIdentity]:
    rows = conn.execute("SELECT * FROM person_identities ORDER BY created_at, id").fetchall()
    if actor.role not in {"admin", "auditor"}:
        rows = [row for row in rows if identity_owned_by_actor(row, actor)]
    if actor.role == "employee":
        rows = [row for row in rows if identity_available_to_employee(conn, row)]
    return [identity_from_row(row, redact_assets=actor.role == "employee") for row in rows]


def get_person_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
) -> PersonIdentity:
    row = read_identity_row(conn, identity_id)
    if actor.role not in {"admin", "auditor"} and not identity_owned_by_actor(row, actor):
        raise character_not_found("PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    if actor.role == "employee" and not identity_available_to_employee(conn, row):
        raise character_not_found("PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    return identity_from_row(row, redact_assets=actor.role == "employee")


def update_person_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    updates: dict[str, object],
) -> PersonIdentity:
    require_character_admin(
        conn,
        actor=actor,
        action="person_identity.update",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    row = read_identity_row(conn, identity_id)
    if str(row["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能修改。")

    assignments: list[str] = []
    parameters: list[object] = []
    if "display_name" in updates:
        assignments.append("display_name = %s")
        parameters.append(
            required_text(
                cast(str, updates["display_name"]),
                "IDENTITY_NAME_REQUIRED",
                "人物显示名不能为空。",
            )
        )
    if "owner_user_id" in updates:
        owner = cast(str | None, updates["owner_user_id"]) or actor.id
        ensure_user_exists(conn, owner)
        assignments.append("owner_user_id = %s")
        parameters.append(owner)
    if "authorization_scope" in updates:
        raw_scope = updates["authorization_scope"]
        if not isinstance(raw_scope, list):
            raise character_error(
                422,
                "IDENTITY_AUTHORIZATION_SCOPE_REQUIRED",
                "至少填写一个肖像授权使用范围。",
            )
        scope = normalize_string_list([str(value) for value in raw_scope])
        if not scope:
            raise character_error(
                422,
                "IDENTITY_AUTHORIZATION_SCOPE_REQUIRED",
                "至少填写一个肖像授权使用范围。",
            )
        assignments.append("authorization_scope = %s")
        parameters.append(encode_json(scope))
    if "authorization_expires_at" in updates:
        assignments.append("authorization_expires_at = %s")
        parameters.append(
            encode_datetime(cast(datetime | None, updates["authorization_expires_at"]))
        )
    if updates.get("authorization_status") == "REVOKED":
        assignments.extend(["authorization_status = 'REVOKED'", "status = 'REVOKED'"])
    if updates.get("status") == "ARCHIVED":
        assignments.append("status = 'ARCHIVED'")

    if assignments:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        parameters.append(identity_id)
        with conn:
            conn.execute(
                f"UPDATE person_identities SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
            refresh_identity_state(conn, identity_id)
    write_audit(
        conn,
        actor=actor,
        action="person_identity.update",
        entity_type="person_identity",
        entity_id=identity_id,
        metadata={"updated_fields": sorted(updates)},
    )
    return get_person_identity(conn, actor=actor, identity_id=identity_id)


def _identity_owner_user_id(identity: sqlite3.Row, *, actor: CurrentUser) -> str:
    """The tenant whose bytes an identity asset belongs to.

    Identity documents and source photos are private material, so this value is
    the deduplication scope owner: identical bytes belonging to a different
    customer must never resolve to the same object, even though the hash matches.
    """
    return str(identity["owner_user_id"] or identity["created_by"] or actor.id)


def _retain_identity_content(
    conn: BusinessConnection,
    *,
    storage: StorageAdapter,
    identity: sqlite3.Row,
    actor: CurrentUser,
    asset_id: str,
    source_key: str,
    content: bytes,
    content_type: str,
    sha256: str,
    size_bytes: int,
) -> tuple[bool, str, content_store.ContentObject]:
    """Register identity bytes, reusing an existing copy when one already exists.

    Returns ``(deduplicated, storage_uri, registered)``. The check runs **before**
    the bytes are written because the verified key embeds the asset id: without
    it an identical authorization file or source photo always lands on a brand
    new key and gets stored twice.

    The scope is always ``user``. Identity material is private, so two customers
    who happen to upload byte-identical files must never share one object — the
    hash matching is exactly the case we have to keep apart.
    """
    registered, deduplicated = content_store.retain_content_object(
        conn,
        sha256=sha256,
        size_bytes=size_bytes,
        content_type=content_type,
        provider=storage.provider,
        bucket=storage.bucket,
        object_key=verified_upload_object_key(
            asset_id=asset_id, digest=sha256, source_key=source_key
        ),
        scope="user",
        owner_user_id=_identity_owner_user_id(identity, actor=actor),
    )
    if deduplicated:
        # This owner already holds these bytes; point the asset at that copy
        # instead of writing a second one.
        return True, registered.storage_uri, registered
    stored = store_verified_upload(
        storage,
        asset_id=asset_id,
        source_key=source_key,
        content=content,
        content_type=content_type,
    )
    return False, stored.uri, registered


def create_identity_upload_intent(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    identity_id: str,
    purpose: IdentityAssetPurpose,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> CreatedIdentityUploadIntent:
    require_character_admin(
        conn,
        actor=actor,
        action=f"person_identity.{purpose}_upload_intent.create",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    identity = read_identity_row(conn, identity_id)
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能上传新文件。")
    if purpose == "source":
        require_current_authorization(identity)
    extension = validate_identity_upload_request(
        purpose=purpose,
        filename=filename,
        content_type=content_type,
        size_bytes=size_bytes,
    )
    asset_id = str(uuid4())
    owner_user_id = str(identity["owner_user_id"] or identity["created_by"] or actor.id)
    storage_key = identity_asset_key(
        owner_user_id=owner_user_id,
        identity_id=identity_id,
        purpose=purpose,
        asset_id=asset_id,
        extension=extension,
    )
    try:
        intent = storage.create_upload_intent(
            storage_key,
            content_type=content_type,
            expires_in=UPLOAD_INTENT_EXPIRES_IN,
            size_bytes=size_bytes,
        )
    except (StorageBackendUnavailable, ValueError) as exc:
        raise character_error(
            503,
            "CHARACTER_STORAGE_UNAVAILABLE",
            "人物素材存储暂时不可用，请稍后重试。",
        ) from exc
    storage_uri = f"{storage.provider}://{storage.bucket}/{intent.key}"
    metadata = {
        "identity_id": identity_id,
        "object_key": intent.key,
        "purpose": purpose,
        "requested_size_bytes": size_bytes,
        "upload_status": "PENDING",
        "intent_expires_at": intent.expires_at.isoformat(),
        "upload_source_uri": storage_uri,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            )
            VALUES (%s, NULL, %s, %s, '', 0, %s, %s, %s)
            """,
            (
                asset_id,
                identity_asset_kind(purpose),
                storage_uri,
                content_type,
                actor.id,
                encode_json(metadata),
            ),
        )
    write_audit(
        conn,
        actor=actor,
        action=f"person_identity.{purpose}_upload_intent.create",
        entity_type="asset",
        entity_id=asset_id,
        metadata={
            "identity_id": identity_id,
            "object_key": intent.key,
            "purpose": purpose,
            "upload_source_uri": storage_uri,
            "intent_expires_at": intent.expires_at.isoformat(),
        },
    )
    return CreatedIdentityUploadIntent(
        asset_id=asset_id,
        identity_id=identity_id,
        purpose=purpose,
        storage_key=intent.key,
        method=intent.method,
        url=intent.url,
        headers=intent.headers,
        expires_at=intent.expires_at.isoformat(),
    )


def complete_authorization_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    identity_id: str,
    asset_id: str,
) -> PersonIdentity:
    require_character_admin(
        conn,
        actor=actor,
        action="person_identity.authorization_upload.complete",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    identity = read_identity_row(conn, identity_id)
    require_identity_accepts_authorization_upload(identity)
    asset, stored, content = read_uploaded_identity_asset(
        conn,
        storage=storage,
        identity_id=identity_id,
        asset_id=asset_id,
        purpose="authorization",
    )
    content_type = str(asset["content_type"] or stored.content_type)
    validate_identity_upload_request(
        purpose="authorization",
        filename=Path(storage_object_ref_from_uri(str(asset["storage_uri"])).key).name,
        content_type=content_type,
        size_bytes=stored.size,
    )
    validate_authorization_content(content, content_type=content_type)
    sha256 = hashlib.sha256(content).hexdigest()
    source_key = storage_object_ref_from_uri(str(asset["storage_uri"])).key
    deduplicated, stored_uri, registered = _retain_identity_content(
        conn,
        storage=storage,
        identity=identity,
        actor=actor,
        asset_id=asset_id,
        source_key=source_key,
        content=content,
        content_type=content_type,
        sha256=sha256,
        size_bytes=stored.size,
    )
    metadata = completed_asset_metadata(asset, stored_uri=stored_uri, stored_size=stored.size)
    metadata["content_deduplicated"] = deduplicated
    state_error: HTTPException | None = None
    with conn:
        update_completed_asset(
            conn,
            asset_id=asset_id,
            storage_uri=stored_uri,
            sha256=sha256,
            size_bytes=stored.size,
            content_type=content_type,
            metadata=metadata,
            content_object_id=registered.id,
        )
        latest_identity = read_identity_row(conn, identity_id, for_update=True)
        try:
            require_identity_accepts_authorization_upload(latest_identity)
        except HTTPException as exc:
            state_error = exc
        if state_error is None:
            authorization_status = (
                "EXPIRED"
                if authorization_is_expired(latest_identity["authorization_expires_at"])
                else "AUTHORIZED"
            )
            next_status = identity_status_after_evidence(
                authorization_status=authorization_status,
                source_quality_status=str(latest_identity["source_quality_status"]),
            )
            conn.execute(
                """
                UPDATE person_identities
                SET authorization_asset_id = %s, authorization_status = %s, status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (asset_id, authorization_status, next_status, identity_id),
            )
    if state_error is not None:
        detail = cast(dict[str, object], state_error.detail)
        write_audit(
            conn,
            actor=actor,
            action="person_identity.authorization_upload.discarded",
            entity_type="asset",
            entity_id=asset_id,
            metadata={
                "identity_id": identity_id,
                "reason": str(detail.get("code", "IDENTITY_STATE_CHANGED")),
            },
        )
        raise state_error
    write_audit(
        conn,
        actor=actor,
        action="person_identity.authorization_upload.complete",
        entity_type="asset",
        entity_id=asset_id,
        metadata={"identity_id": identity_id, "sha256": sha256},
    )
    return get_person_identity(conn, actor=actor, identity_id=identity_id)


def complete_source_upload(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    inspector: SourceImageInspector,
    identity_id: str,
    asset_id: str,
) -> CompletedSourceImage:
    require_character_admin(
        conn,
        actor=actor,
        action="person_identity.source_upload.complete",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    identity = read_identity_row(conn, identity_id)
    require_current_authorization(identity)
    asset, stored, content = read_uploaded_identity_asset(
        conn,
        storage=storage,
        identity_id=identity_id,
        asset_id=asset_id,
        purpose="source",
    )
    content_type = str(asset["content_type"] or stored.content_type)
    validate_identity_upload_request(
        purpose="source",
        filename=Path(storage_object_ref_from_uri(str(asset["storage_uri"])).key).name,
        content_type=content_type,
        size_bytes=stored.size,
    )
    width, height = image_dimensions(content, content_type=content_type)
    sha256 = hashlib.sha256(content).hexdigest()
    deduplicated, stored_uri, registered = _retain_identity_content(
        conn,
        storage=storage,
        identity=identity,
        actor=actor,
        asset_id=asset_id,
        source_key=storage_object_ref_from_uri(str(asset["storage_uri"])).key,
        content=content,
        content_type=content_type,
        sha256=sha256,
        size_bytes=stored.size,
    )
    started = time.monotonic()
    try:
        inspection = inspector.inspect(content, content_type=content_type)
    except SourceImageInspectorFailed as exc:
        provider_state_error = persist_source_inspection_failure(
            conn,
            asset=asset,
            stored_uri=stored_uri,
            stored_size=stored.size,
            sha256=sha256,
            content_type=content_type,
            identity_id=identity_id,
            actor=actor,
            latency_ms=int((time.monotonic() - started) * 1000),
            content_object_id=registered.id,
        )
        if provider_state_error is not None:
            raise provider_state_error
        raise character_error(
            503,
            "SOURCE_IMAGE_INSPECTOR_UNAVAILABLE",
            "真人源图语义质检暂时不可用，请稍后重新完成上传。",
        ) from exc
    quality = evaluate_source_image_quality(width=width, height=height, inspection=inspection)
    metadata = completed_asset_metadata(asset, stored_uri=stored_uri, stored_size=stored.size)
    metadata["quality"] = quality.model_dump(mode="json")
    metadata["content_deduplicated"] = deduplicated
    state_error: HTTPException | None = None
    identity_source_updated = False
    with conn:
        update_completed_asset(
            conn,
            asset_id=asset_id,
            storage_uri=stored_uri,
            sha256=sha256,
            size_bytes=stored.size,
            content_type=content_type,
            metadata=metadata,
            content_object_id=registered.id,
        )
        latest_identity = read_identity_row(conn, identity_id, for_update=True)
        try:
            require_current_authorization(latest_identity)
        except HTTPException as exc:
            state_error = exc
        if state_error is None and (
            quality.passed or not identity_has_usable_source(latest_identity)
        ):
            conn.execute(
                """
                UPDATE person_identities
                SET source_asset_id = %s, source_quality_status = %s, status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (
                    asset_id,
                    "PASSED" if quality.passed else "FAILED",
                    "ACTIVE" if quality.passed else "DRAFT",
                    identity_id,
                ),
            )
            identity_source_updated = True
        conn.execute(
            """
            INSERT INTO external_call_logs (
                id, generation_task_id, provider, model, endpoint_name,
                latency_ms, request_hash, error_code
            )
            VALUES (%s, NULL, %s, %s, 'source_image.inspect', %s, %s, NULL)
            """,
            (
                str(uuid4()),
                inspection.provider,
                inspection.model,
                int((time.monotonic() - started) * 1000),
                sha256,
            ),
        )
    if state_error is not None:
        write_audit(
            conn,
            actor=actor,
            action="person_identity.source_upload.discarded",
            entity_type="asset",
            entity_id=asset_id,
            metadata={"identity_id": identity_id, "reason": "IDENTITY_STATE_CHANGED"},
        )
        raise state_error
    write_audit(
        conn,
        actor=actor,
        action="person_identity.source_upload.complete",
        entity_type="asset",
        entity_id=asset_id,
        metadata={
            "identity_id": identity_id,
            "issue_codes": quality.issue_codes,
            "quality_passed": quality.passed,
            "identity_source_updated": identity_source_updated,
            "sha256": sha256,
        },
    )
    if not quality.passed:
        raise character_error(
            422,
            "SOURCE_IMAGE_QUALITY_FAILED",
            "真人源图未通过质检，请根据问题更换照片后重试。",
            issue_codes=quality.issue_codes,
            issues=source_quality_issue_messages(quality.issue_codes),
        )
    return CompletedSourceImage(
        identity=get_person_identity(conn, actor=actor, identity_id=identity_id),
        asset_id=asset_id,
        sha256=sha256,
        size_bytes=stored.size,
        content_type=content_type,
        quality=quality,
    )


def create_character_persona(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    values: dict[str, object],
) -> CharacterPersona:
    require_character_admin(
        conn,
        actor=actor,
        action="character_persona.create",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    identity = read_identity_row(conn, identity_id)
    require_identity_active(identity)
    persona_id = str(uuid4())
    normalized = normalize_persona_values(values, require_name=True)
    with conn:
        conn.execute(
            """
            INSERT INTO character_personas (
                id, identity_id, name, occupation, scene_description,
                appearance_constraints_json, costume_description,
                default_background, positive_prompt, negative_prompt,
                usage_scope_json, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                persona_id,
                identity_id,
                normalized["name"],
                normalized["occupation"],
                normalized["scene_description"],
                encode_json(normalized["appearance_constraints_json"]),
                normalized["costume_description"],
                normalized["default_background"],
                normalized["positive_prompt"],
                normalized["negative_prompt"],
                encode_json(normalized["usage_scope_json"]),
                actor.id,
            ),
        )
    write_audit(
        conn,
        actor=actor,
        action="character_persona.create",
        entity_type="character_persona",
        entity_id=persona_id,
        metadata={"identity_id": identity_id},
    )
    return get_character_persona(conn, actor=actor, persona_id=persona_id)


def list_character_personas(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
) -> list[CharacterPersona]:
    identity = read_identity_row(conn, identity_id)
    if actor.role not in {"admin", "auditor"} and not identity_owned_by_actor(identity, actor):
        return []
    if actor.role == "employee" and not effective_identity_is_active(identity):
        return []
    rows = conn.execute(
        "SELECT * FROM character_personas WHERE identity_id = %s ORDER BY created_at, id",
        (identity_id,),
    ).fetchall()
    if actor.role == "employee":
        rows = [row for row in rows if persona_has_published_version(conn, str(row["id"]))]
    return [persona_from_row(row) for row in rows]


def get_character_persona(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
) -> CharacterPersona:
    row = read_persona_row(conn, persona_id)
    identity = read_identity_row(conn, str(row["identity_id"]))
    if actor.role not in {"admin", "auditor"} and not identity_owned_by_actor(identity, actor):
        raise character_not_found("CHARACTER_PERSONA_NOT_FOUND", "角色人设不存在或不可用。")
    if actor.role == "employee" and (
        not effective_identity_is_active(identity)
        or not persona_has_published_version(conn, persona_id)
    ):
        raise character_not_found("CHARACTER_PERSONA_NOT_FOUND", "角色人设不存在或不可用。")
    return persona_from_row(row)


def update_character_persona(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
    updates: dict[str, object],
) -> CharacterPersona:
    require_character_admin(
        conn,
        actor=actor,
        action="character_persona.update",
        entity_type="character_persona",
        entity_id=persona_id,
    )
    row = read_persona_row(conn, persona_id)
    identity = read_identity_row(conn, str(row["identity_id"]))
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份的人设不能修改。")
    normalized = normalize_persona_values(updates, require_name=False)
    assignments: list[str] = []
    parameters: list[object] = []
    for key, value in normalized.items():
        assignments.append(f"{key} = %s")
        parameters.append(encode_json(value) if key.endswith("_json") else value)
    if assignments:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
        parameters.append(persona_id)
        with conn:
            conn.execute(
                f"UPDATE character_personas SET {', '.join(assignments)} WHERE id = %s",
                parameters,
            )
    write_audit(
        conn,
        actor=actor,
        action="character_persona.update",
        entity_type="character_persona",
        entity_id=persona_id,
        metadata={"updated_fields": sorted(updates)},
    )
    return get_character_persona(conn, actor=actor, persona_id=persona_id)


def delete_character_persona(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
) -> None:
    require_character_admin(
        conn,
        actor=actor,
        action="character_persona.delete",
        entity_type="character_persona",
        entity_id=persona_id,
    )
    read_persona_row(conn, persona_id)
    has_versions = conn.execute(
        "SELECT 1 FROM character_versions WHERE persona_id = %s LIMIT 1",
        (persona_id,),
    ).fetchone()
    if has_versions is not None:
        raise character_error(
            409,
            "CHARACTER_PERSONA_HAS_VERSIONS",
            "已创建角色版本的人设不能删除；请保留其历史快照。",
        )
    with conn:
        conn.execute("DELETE FROM character_personas WHERE id = %s", (persona_id,))
    write_audit(
        conn,
        actor=actor,
        action="character_persona.delete",
        entity_type="character_persona",
        entity_id=persona_id,
    )


def create_character_version(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
    provider: str,
    model: str,
    generation_params_json: dict[str, object],
) -> CharacterVersion:
    require_character_admin(
        conn,
        actor=actor,
        action="character_version.create",
        entity_type="character_persona",
        entity_id=persona_id,
    )
    persona = read_persona_row(conn, persona_id)
    identity = read_identity_row(conn, str(persona["identity_id"]))
    require_identity_active(identity)
    source_asset_id = cast(str, identity["source_asset_id"])
    source_asset = conn.execute(
        "SELECT sha256 FROM assets WHERE id = %s",
        (source_asset_id,),
    ).fetchone()
    if source_asset is None or not str(source_asset["sha256"]):
        raise character_error(
            409,
            "IDENTITY_SOURCE_NOT_READY",
            "真人源图尚未完成，不能创建角色版本。",
        )
    clean_provider = required_text(
        provider,
        "CHARACTER_PROVIDER_REQUIRED",
        "角色图片 Provider 不能为空。",
    )
    clean_model = required_text(
        model,
        "CHARACTER_MODEL_REQUIRED",
        "角色图片模型不能为空。",
    )
    version_id = str(uuid4())
    version_number = next_character_version_number(conn, persona_id)
    snapshot = persona_snapshot(persona)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO character_versions (
                    id, persona_id, version_number, status, source_asset_id,
                    source_sha256, persona_snapshot_json, provider, model,
                    generation_params_json, template_version, template_hash,
                    required_view_types_json, created_by
                )
                VALUES (%s, %s, %s, 'DRAFT', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    persona_id,
                    version_number,
                    source_asset_id,
                    str(source_asset["sha256"]),
                    encode_json(snapshot),
                    clean_provider,
                    clean_model,
                    encode_json(generation_params_json),
                    CHARACTER_TEMPLATE_VERSION,
                    CHARACTER_TEMPLATE_HASH,
                    encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
                    actor.id,
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise character_error(
            409,
            "CHARACTER_VERSION_CONFLICT",
            "角色版本号发生冲突，请重新创建。",
        ) from exc
    write_audit(
        conn,
        actor=actor,
        action="character_version.create",
        entity_type="character_version",
        entity_id=version_id,
        metadata={"persona_id": persona_id, "version_number": version_number},
    )
    return get_character_version(conn, actor=actor, version_id=version_id)


def list_character_versions(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
) -> list[CharacterVersion]:
    persona = read_persona_row(conn, persona_id)
    identity = read_identity_row(conn, str(persona["identity_id"]))
    if actor.role not in {"admin", "auditor"} and not identity_owned_by_actor(identity, actor):
        return []
    if actor.role == "employee" and not effective_identity_is_active(identity):
        return []
    if actor.role == "employee":
        rows = conn.execute(
            """
            SELECT * FROM character_versions
            WHERE persona_id = %s AND status = 'PUBLISHED'
            ORDER BY version_number
            """,
            (persona_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM character_versions
            WHERE persona_id = %s
            ORDER BY version_number
            """,
            (persona_id,),
        ).fetchall()
    return [version_from_row(row, redact_source=actor.role == "employee") for row in rows]


def get_character_version(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
) -> CharacterVersion:
    row = read_version_row(conn, version_id)
    persona = read_persona_row(conn, str(row["persona_id"]))
    identity = read_identity_row(conn, str(persona["identity_id"]))
    if actor.role not in {"admin", "auditor"} and not identity_owned_by_actor(identity, actor):
        raise character_not_found("CHARACTER_VERSION_NOT_FOUND", "角色版本不存在或不可用。")
    if actor.role == "employee" and (
        str(row["status"]) != "PUBLISHED" or not effective_identity_is_active(identity)
    ):
        raise character_not_found("CHARACTER_VERSION_NOT_FOUND", "角色版本不存在或不可用。")
    return version_from_row(row, redact_source=actor.role == "employee")


def archive_character_version(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
) -> CharacterVersion:
    require_character_admin(
        conn,
        actor=actor,
        action="character_version.archive",
        entity_type="character_version",
        entity_id=version_id,
    )
    read_version_row(conn, version_id)
    with conn:
        conn.execute(
            "UPDATE character_versions SET status = 'ARCHIVED' WHERE id = %s",
            (version_id,),
        )
    write_audit(
        conn,
        actor=actor,
        action="character_version.archive",
        entity_type="character_version",
        entity_id=version_id,
    )
    return get_character_version(conn, actor=actor, version_id=version_id)


def identity_asset_key(
    *,
    owner_user_id: str,
    identity_id: str,
    purpose: IdentityAssetPurpose,
    asset_id: str,
    extension: str,
) -> str:
    for value in (owner_user_id, identity_id, asset_id):
        validate_key_segment(value)
    if purpose not in {"authorization", "source"}:
        raise ValueError("unsupported identity asset purpose")
    allowed_extensions = (
        {".jpg", ".png", ".webp"} if purpose == "source" else {".pdf", ".jpg", ".png"}
    )
    if extension not in allowed_extensions:
        raise ValueError("unsupported identity asset extension")
    return f"users/{owner_user_id}/identities/{identity_id}/{purpose}/{asset_id}{extension}"


def generated_character_asset_key(
    *,
    owner_user_id: str,
    persona_id: str,
    version_id: str,
    view_type: RequiredCharacterViewType,
    asset_id: str,
) -> str:
    return character_asset_key(
        owner_user_id=owner_user_id,
        persona_id=persona_id,
        version_id=version_id,
        purpose="generated",
        view_type=view_type,
        asset_id=asset_id,
    )


def approved_character_asset_key(
    *,
    owner_user_id: str,
    persona_id: str,
    version_id: str,
    view_type: RequiredCharacterViewType,
    asset_id: str,
) -> str:
    return character_asset_key(
        owner_user_id=owner_user_id,
        persona_id=persona_id,
        version_id=version_id,
        purpose="approved",
        view_type=view_type,
        asset_id=asset_id,
    )


def character_asset_key(
    *,
    owner_user_id: str,
    persona_id: str,
    version_id: str,
    purpose: CharacterAssetPurpose,
    view_type: RequiredCharacterViewType,
    asset_id: str,
) -> str:
    for value in (owner_user_id, persona_id, version_id, view_type, asset_id):
        validate_key_segment(value)
    if purpose not in {"generated", "approved"}:
        raise ValueError("unsupported character asset purpose")
    return (
        f"users/{owner_user_id}/personas/{persona_id}/versions/{version_id}/"
        f"{purpose}/{view_type}/{asset_id}.png"
    )


def image_dimensions(content: bytes, *, content_type: str) -> tuple[int, int]:
    if content_type == "image/png":
        if len(content) < 24 or content[:8] != b"\x89PNG\r\n\x1a\n" or content[12:16] != b"IHDR":
            raise character_error(
                422,
                "SOURCE_IMAGE_INVALID",
                "PNG 文件内容无效，请重新导出后上传。",
            )
        width, height = struct.unpack(">II", content[16:24])
        return validate_image_dimensions(width, height)
    if content_type == "image/jpeg":
        return jpeg_dimensions(content)
    raise character_error(
        415,
        "SOURCE_IMAGE_TYPE_UNSUPPORTED",
        "真人源图仅支持 JPG 或 PNG。",
    )


def jpeg_dimensions(content: bytes) -> tuple[int, int]:
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        raise character_error(422, "SOURCE_IMAGE_INVALID", "JPG 文件内容无效。")
    offset = 2
    start_of_frame_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset + 3 < len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        marker = content[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(content):
            break
        segment_length = struct.unpack(">H", content[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(content):
            break
        if marker in start_of_frame_markers and segment_length >= 7:
            height, width = struct.unpack(">HH", content[offset + 3 : offset + 7])
            return validate_image_dimensions(width, height)
        offset += segment_length
    raise character_error(
        422,
        "SOURCE_IMAGE_INVALID",
        "无法读取 JPG 像素尺寸，请重新导出后上传。",
    )


def evaluate_source_image_quality(
    *,
    width: int,
    height: int,
    inspection: SourceImageInspection,
) -> SourceImageQualityResult:
    issues: list[str] = []
    if width < MIN_SOURCE_IMAGE_WIDTH or height < MIN_SOURCE_IMAGE_HEIGHT:
        issues.append("IMAGE_DIMENSIONS_TOO_SMALL")
    if inspection.person_count == 0:
        issues.append("PERSON_NOT_FOUND")
    elif inspection.person_count > 1:
        issues.append("MULTIPLE_PEOPLE")
    if inspection.face_count != 1:
        issues.append("FACE_COUNT_INVALID")
    if not inspection.face_visible:
        issues.append("FACE_NOT_VISIBLE")
    if inspection.sharpness_score < MIN_SOURCE_SHARPNESS_SCORE:
        issues.append("IMAGE_NOT_SHARP")
    if inspection.occlusion_detected:
        issues.append("FACE_OCCLUDED")
    if inspection.watermark_detected:
        issues.append("WATERMARK_DETECTED")
    return SourceImageQualityResult(
        passed=not issues,
        width=width,
        height=height,
        person_count=inspection.person_count,
        face_count=inspection.face_count,
        face_visible=inspection.face_visible,
        sharpness_score=inspection.sharpness_score,
        occlusion_detected=inspection.occlusion_detected,
        watermark_detected=inspection.watermark_detected,
        issue_codes=issues,
        notes=inspection.notes,
        provider=inspection.provider,
        model=inspection.model,
    )


def read_identity_row(
    conn: BusinessConnection, identity_id: str, *, for_update: bool = False
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM person_identities WHERE id = %s" + (" FOR UPDATE" if for_update else ""),
        (identity_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    return cast(sqlite3.Row, row)


def read_persona_row(conn: BusinessConnection, persona_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM character_personas WHERE id = %s",
        (persona_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("CHARACTER_PERSONA_NOT_FOUND", "角色人设不存在或不可用。")
    return cast(sqlite3.Row, row)


def read_version_row(conn: BusinessConnection, version_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM character_versions WHERE id = %s",
        (version_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("CHARACTER_VERSION_NOT_FOUND", "角色版本不存在或不可用。")
    return cast(sqlite3.Row, row)


def identity_from_row(row: sqlite3.Row, *, redact_assets: bool) -> PersonIdentity:
    authorization_status, status = effective_identity_state(row)
    return PersonIdentity(
        id=str(row["id"]),
        owner_user_id=None if row["owner_user_id"] is None else str(row["owner_user_id"]),
        display_name=str(row["display_name"]),
        authorization_status=cast(Any, authorization_status),
        authorization_asset_id=(
            None
            if redact_assets or row["authorization_asset_id"] is None
            else str(row["authorization_asset_id"])
        ),
        authorization_scope=decode_string_list(row["authorization_scope"]),
        authorization_expires_at=parse_optional_datetime(row["authorization_expires_at"]),
        source_asset_id=(
            None if redact_assets or row["source_asset_id"] is None else str(row["source_asset_id"])
        ),
        source_quality_status=cast(Any, str(row["source_quality_status"])),
        status=cast(Any, status),
        created_by=None if row["created_by"] is None else str(row["created_by"]),
        created_at=parse_datetime(str(row["created_at"])),
        updated_at=parse_datetime(str(row["updated_at"])),
    )


def persona_from_row(row: sqlite3.Row) -> CharacterPersona:
    return CharacterPersona(
        id=str(row["id"]),
        identity_id=str(row["identity_id"]),
        name=str(row["name"]),
        occupation=optional_text(row["occupation"]),
        scene_description=optional_text(row["scene_description"]),
        appearance_constraints_json=decode_object(row["appearance_constraints_json"]),
        costume_description=optional_text(row["costume_description"]),
        default_background=optional_text(row["default_background"]),
        positive_prompt=optional_text(row["positive_prompt"]),
        negative_prompt=optional_text(row["negative_prompt"]),
        usage_scope_json=decode_string_list(row["usage_scope_json"]),
        created_by=None if row["created_by"] is None else str(row["created_by"]),
        created_at=parse_datetime(str(row["created_at"])),
        updated_at=parse_datetime(str(row["updated_at"])),
    )


def version_from_row(row: sqlite3.Row, *, redact_source: bool) -> CharacterVersion:
    return CharacterVersion(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        version_number=int(row["version_number"]),
        status=cast(Any, str(row["status"])),
        source_asset_id=(
            None if redact_source or row["source_asset_id"] is None else str(row["source_asset_id"])
        ),
        source_sha256=(
            None if redact_source or row["source_sha256"] is None else str(row["source_sha256"])
        ),
        persona_snapshot_json=decode_object(row["persona_snapshot_json"]),
        provider=optional_text(row["provider"]),
        model=optional_text(row["model"]),
        generation_params_json=decode_object(row["generation_params_json"]),
        template_version=optional_text(row["template_version"]),
        template_hash=optional_text(row["template_hash"]),
        required_view_types_json=cast(
            list[RequiredCharacterViewType],
            decode_string_list(row["required_view_types_json"]),
        ),
        published_by=optional_text(row["published_by"]),
        published_at=parse_optional_datetime(row["published_at"]),
        publication_snapshot_json=(
            None
            if row["publication_snapshot_json"] is None
            else decode_object(row["publication_snapshot_json"])
        ),
        publication_hash=optional_text(row["publication_hash"]),
        created_by=optional_text(row["created_by"]),
        created_at=parse_datetime(str(row["created_at"])),
    )


def require_character_admin(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: str,
) -> None:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin"},
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
    )


def require_current_authorization(identity: sqlite3.Row) -> None:
    authorization_status, status = effective_identity_state(identity)
    if (
        authorization_status != "AUTHORIZED"
        or status in {"EXPIRED", "REVOKED", "ARCHIVED"}
        or identity["authorization_asset_id"] is None
    ):
        raise character_error(
            409,
            "IDENTITY_AUTHORIZATION_REQUIRED",
            "请先完成当前有效的肖像授权附件，再上传真人源图。",
        )


def require_identity_accepts_authorization_upload(identity: sqlite3.Row) -> None:
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能更新授权。")
    if str(identity["status"]) == "REVOKED" or str(identity["authorization_status"]) == "REVOKED":
        raise character_error(409, "IDENTITY_REVOKED", "已撤销人物身份不能更新授权。")


def require_identity_active(identity: sqlite3.Row) -> None:
    if not effective_identity_is_active(identity):
        raise character_error(
            409,
            "IDENTITY_NOT_ACTIVE",
            "人物身份必须具有有效授权和已通过质检的真人源图。",
        )


def effective_identity_state(row: sqlite3.Row) -> tuple[str, str]:
    return effective_identity_state_values(
        status=row["status"],
        authorization_status=row["authorization_status"],
        authorization_expires_at=row["authorization_expires_at"],
        source_quality_status=row["source_quality_status"],
    )


def effective_identity_is_active(row: sqlite3.Row) -> bool:
    authorization_status, status = effective_identity_state(row)
    return authorization_status == "AUTHORIZED" and status == "ACTIVE"


def identity_has_usable_source(row: sqlite3.Row) -> bool:
    return row["source_asset_id"] is not None and effective_identity_is_active(row)


def identity_owned_by_actor(row: sqlite3.Row, actor: CurrentUser) -> bool:
    owner_user_id = row["owner_user_id"]
    return owner_user_id is not None and str(owner_user_id) == actor.id


def identity_available_to_employee(conn: BusinessConnection, row: sqlite3.Row) -> bool:
    if not effective_identity_is_active(row):
        return False
    return (
        conn.execute(
            """
            SELECT 1
            FROM character_versions AS version
            JOIN character_personas AS persona ON persona.id = version.persona_id
            WHERE persona.identity_id = %s AND version.status = 'PUBLISHED'
            LIMIT 1
            """,
            (str(row["id"]),),
        ).fetchone()
        is not None
    )


def persona_has_published_version(conn: BusinessConnection, persona_id: str) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM character_versions
            WHERE persona_id = %s AND status = 'PUBLISHED'
            LIMIT 1
            """,
            (persona_id,),
        ).fetchone()
        is not None
    )


def refresh_identity_state(conn: BusinessConnection, identity_id: str) -> None:
    row = read_identity_row(conn, identity_id)
    if str(row["status"]) in {"ARCHIVED", "REVOKED"}:
        return
    authorization_status, status = effective_identity_state(row)
    conn.execute(
        """
        UPDATE person_identities
        SET authorization_status = %s, status = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        """,
        (authorization_status, status, identity_id),
    )


def read_uploaded_identity_asset(
    conn: BusinessConnection,
    *,
    storage: StorageAdapter,
    identity_id: str,
    asset_id: str,
    purpose: IdentityAssetPurpose,
) -> tuple[sqlite3.Row, Any, bytes]:
    asset = conn.execute("SELECT * FROM assets WHERE id = %s", (asset_id,)).fetchone()
    if asset is None:
        raise character_not_found("ASSET_NOT_FOUND", "上传记录不存在。")
    metadata = decode_object(asset["metadata_json"])
    if metadata.get("upload_status") == "EXPIRED":
        raise character_error(409, "UPLOAD_EXPIRED", "上传已过期，请重新创建上传。")
    if (
        asset["project_id"] is not None
        or str(asset["kind"]) != identity_asset_kind(purpose)
        or metadata.get("identity_id") != identity_id
        or metadata.get("purpose") != purpose
    ):
        raise character_error(
            409,
            "IDENTITY_ASSET_MISMATCH",
            "上传记录不属于当前人物身份或用途。",
        )
    try:
        reference = storage_object_ref_from_uri(str(asset["storage_uri"]))
        require_storage_match(storage, reference)
        stored = storage.head_object(reference.key)
        if stored is None:
            raise character_error(
                409,
                "UPLOAD_OBJECT_MISSING",
                "上传对象尚未就绪，请等待上传完成后重试。",
            )
        expected_size = (
            int(asset["size_bytes"])
            if int(asset["size_bytes"]) > 0
            else metadata.get("requested_size_bytes")
        )
        if not isinstance(expected_size, int) or stored.size != expected_size:
            raise character_error(409, "UPLOAD_SIZE_MISMATCH", "上传文件大小不一致。")
        maximum = MAX_SOURCE_IMAGE_BYTES if purpose == "source" else MAX_AUTHORIZATION_BYTES
        if expected_size > maximum:
            raise character_error(413, "UPLOAD_TOO_LARGE", "上传文件超出大小限制。")
        content = read_uploaded_object(
            storage, reference.key, expected_size=expected_size, max_bytes=maximum
        )
        if str(asset["sha256"]) and hashlib.sha256(content).hexdigest() != str(asset["sha256"]):
            raise character_error(409, "UPLOAD_HASH_MISMATCH", "已完成上传内容发生变化。")
    except UploadedObjectSizeMismatch as exc:
        raise character_error(409, "UPLOAD_SIZE_MISMATCH", "上传文件大小不一致。") from exc
    except HTTPException:
        raise
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        raise character_error(
            503,
            "CHARACTER_STORAGE_UNAVAILABLE",
            "人物素材存储暂时不可用，请稍后重试。",
        ) from exc
    return cast(sqlite3.Row, asset), stored, content


def validate_identity_upload_request(
    *,
    purpose: IdentityAssetPurpose,
    filename: str,
    content_type: str,
    size_bytes: int,
) -> str:
    allowed = ALLOWED_SOURCE_TYPES if purpose == "source" else ALLOWED_AUTHORIZATION_TYPES
    expected_extension = allowed.get(content_type)
    suffix = Path(filename).suffix.lower()
    if expected_extension is None or suffix not in (
        {".jpg", ".jpeg"} if expected_extension == ".jpg" else {expected_extension}
    ):
        code = (
            "SOURCE_IMAGE_TYPE_UNSUPPORTED"
            if purpose == "source"
            else "AUTHORIZATION_FILE_TYPE_UNSUPPORTED"
        )
        message = (
            "真人源图仅支持 JPG 或 PNG。"
            if purpose == "source"
            else "肖像授权附件仅支持 PDF、JPG 或 PNG。"
        )
        raise character_error(415, code, message)
    maximum = MAX_SOURCE_IMAGE_BYTES if purpose == "source" else MAX_AUTHORIZATION_BYTES
    if size_bytes <= 0:
        raise character_error(422, "UPLOAD_SIZE_INVALID", "上传文件不能为空。")
    if size_bytes > maximum:
        raise character_error(
            413,
            "UPLOAD_TOO_LARGE",
            f"上传文件不能超过 {maximum // (1024 * 1024)}MB。",
        )
    return expected_extension


def validate_authorization_content(content: bytes, *, content_type: str) -> None:
    if content_type == "application/pdf" and not content.startswith(b"%PDF-"):
        raise character_error(422, "AUTHORIZATION_FILE_INVALID", "PDF 授权附件内容无效。")
    if content_type.startswith("image/"):
        image_dimensions(content, content_type=content_type)


def completed_asset_metadata(
    asset: sqlite3.Row,
    *,
    stored_uri: str,
    stored_size: int,
) -> dict[str, object]:
    metadata = decode_object(asset["metadata_json"])
    metadata.update(
        {
            "_completion_expected_state": [
                str(asset["storage_uri"]),
                str(asset["sha256"]),
                int(asset["size_bytes"]),
            ],
            "stored_size_bytes": stored_size,
            "storage_uri_scheme": storage_object_ref_from_uri(stored_uri).provider,
            "upload_status": "COMPLETE",
        }
    )
    return metadata


def update_completed_asset(
    conn: BusinessConnection,
    *,
    asset_id: str,
    storage_uri: str,
    sha256: str,
    size_bytes: int,
    content_type: str,
    metadata: dict[str, object],
    content_object_id: str | None = None,
) -> None:
    metadata = dict(metadata)
    expected = metadata.pop("_completion_expected_state", None)
    current = conn.execute(
        "SELECT storage_uri, sha256, size_bytes, metadata_json FROM assets WHERE id=%s FOR UPDATE",
        (asset_id,),
    ).fetchone()
    if current is None or expected != [str(current[0]), str(current[1]), int(current[2])]:
        raise character_error(409, "UPLOAD_STATE_CHANGED", "上传记录已变化，请重试完成上传。")
    if decode_object(current[3]).get("upload_status") == "EXPIRED":
        raise character_error(409, "UPLOAD_EXPIRED", "上传已过期，请重新创建上传。")
    # ``content_object_id`` is optional so callers that predates the content
    # registry keep working: without it the row simply stays unregistered.
    if content_object_id is None:
        conn.execute(
            """
            UPDATE assets
            SET storage_uri = %s, sha256 = %s, size_bytes = %s, content_type = %s,
                metadata_json = %s
            WHERE id = %s
            """,
            (storage_uri, sha256, size_bytes, content_type, encode_json(metadata), asset_id),
        )
        return
    conn.execute(
        """
        UPDATE assets
        SET storage_uri = %s, sha256 = %s, size_bytes = %s, content_type = %s,
            metadata_json = %s, content_object_id = %s
        WHERE id = %s
        """,
        (
            storage_uri,
            sha256,
            size_bytes,
            content_type,
            encode_json(metadata),
            content_object_id,
            asset_id,
        ),
    )


def persist_source_inspection_failure(
    conn: BusinessConnection,
    *,
    asset: sqlite3.Row,
    stored_uri: str,
    stored_size: int,
    sha256: str,
    content_type: str,
    identity_id: str,
    actor: CurrentUser,
    latency_ms: int,
    content_object_id: str | None = None,
) -> HTTPException | None:
    metadata = completed_asset_metadata(asset, stored_uri=stored_uri, stored_size=stored_size)
    metadata["inspection_status"] = "ERROR"
    state_error: HTTPException | None = None
    identity_source_updated = False
    with conn:
        update_completed_asset(
            conn,
            asset_id=str(asset["id"]),
            storage_uri=stored_uri,
            sha256=sha256,
            size_bytes=stored_size,
            content_type=content_type,
            metadata=metadata,
            content_object_id=content_object_id,
        )
        latest_identity = read_identity_row(conn, identity_id, for_update=True)
        try:
            require_current_authorization(latest_identity)
        except HTTPException as exc:
            state_error = exc
        if state_error is None and not identity_has_usable_source(latest_identity):
            conn.execute(
                """
                UPDATE person_identities
                SET source_asset_id = %s, source_quality_status = 'PENDING', status = 'DRAFT',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (str(asset["id"]), identity_id),
            )
            identity_source_updated = True
        conn.execute(
            """
            INSERT INTO external_call_logs (
                id, generation_task_id, provider, model, endpoint_name,
                latency_ms, request_hash, error_code, error_message_redacted
            )
            VALUES (
                %s, NULL, 'source-image-inspector', NULL, 'source_image.inspect',
                %s, %s, 'SOURCE_IMAGE_INSPECTOR_UNAVAILABLE', 'source image inspection failed'
            )
            """,
            (str(uuid4()), latency_ms, sha256),
        )
    write_audit(
        conn,
        actor=actor,
        action="person_identity.source_upload.inspect_failed",
        entity_type="asset",
        entity_id=str(asset["id"]),
        metadata={
            "identity_id": identity_id,
            "identity_state_changed": state_error is not None,
            "identity_source_updated": identity_source_updated,
        },
    )
    return state_error


def normalize_persona_values(
    values: dict[str, object],
    *,
    require_name: bool,
) -> dict[str, object]:
    result: dict[str, object] = {}
    if require_name or "name" in values:
        result["name"] = required_text(
            cast(str, values.get("name", "")),
            "CHARACTER_PERSONA_NAME_REQUIRED",
            "角色人设名称不能为空。",
        )
    for key in (
        "occupation",
        "scene_description",
        "costume_description",
        "default_background",
        "positive_prompt",
        "negative_prompt",
    ):
        if key in values:
            value = values[key]
            result[key] = None if value is None else str(value).strip() or None
    if "appearance_constraints_json" in values or require_name:
        constraints = values.get("appearance_constraints_json", {})
        if not isinstance(constraints, dict):
            raise character_error(
                422,
                "CHARACTER_PERSONA_APPEARANCE_INVALID",
                "外观约束必须是 JSON 对象。",
            )
        result["appearance_constraints_json"] = constraints
    if "usage_scope_json" in values or require_name:
        scopes = values.get("usage_scope_json", [])
        if not isinstance(scopes, list):
            raise character_error(
                422,
                "CHARACTER_PERSONA_SCOPE_INVALID",
                "人设使用范围必须是字符串数组。",
            )
        result["usage_scope_json"] = normalize_string_list([str(item) for item in scopes])
    return result


def persona_snapshot(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": str(row["id"]),
        "identity_id": str(row["identity_id"]),
        "name": str(row["name"]),
        "occupation": optional_text(row["occupation"]),
        "scene_description": optional_text(row["scene_description"]),
        "appearance_constraints_json": decode_object(row["appearance_constraints_json"]),
        "costume_description": optional_text(row["costume_description"]),
        "default_background": optional_text(row["default_background"]),
        "positive_prompt": optional_text(row["positive_prompt"]),
        "negative_prompt": optional_text(row["negative_prompt"]),
        "usage_scope_json": decode_string_list(row["usage_scope_json"]),
    }


def next_character_version_number(conn: BusinessConnection, persona_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM character_versions
        WHERE persona_id = %s
        """,
        (persona_id,),
    ).fetchone()
    return int(row[0])


def validate_key_segment(value: str) -> None:
    if not SAFE_KEY_SEGMENT.fullmatch(value):
        raise ValueError("object key segment contains unsupported characters")


def validate_image_dimensions(width: int, height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0 or width > 50_000 or height > 50_000:
        raise character_error(422, "SOURCE_IMAGE_INVALID", "图片像素尺寸无效。")
    return width, height


def identity_status_after_evidence(
    *,
    authorization_status: str,
    source_quality_status: str,
) -> str:
    if authorization_status == "EXPIRED":
        return "EXPIRED"
    if authorization_status == "REVOKED":
        return "REVOKED"
    if (
        authorization_status == "AUTHORIZED"
        and source_quality_status in USABLE_SOURCE_QUALITY_STATUSES
    ):
        return "ACTIVE"
    return "DRAFT"


def identity_asset_kind(purpose: IdentityAssetPurpose) -> str:
    return "character_authorization" if purpose == "authorization" else "character_source_image"


def required_text(value: object, code: str, message: str) -> str:
    if not isinstance(value, str):
        raise character_error(422, code, message)
    normalized = value.strip()
    if not normalized:
        raise character_error(422, code, message)
    return normalized


def normalize_string_list(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def ensure_user_exists(conn: BusinessConnection, user_id: str) -> None:
    row = conn.execute("SELECT 1 FROM users WHERE id = %s AND is_active = 1", (user_id,)).fetchone()
    if row is None:
        raise character_error(422, "IDENTITY_OWNER_NOT_FOUND", "人物归属用户不存在或未启用。")


def encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def decode_object(value: object) -> dict[str, object]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def decode_string_list(value: object) -> list[str]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


def encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_optional_datetime(value: object) -> datetime | None:
    return None if value is None else parse_datetime(str(value))


def optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def source_quality_issue_messages(issue_codes: list[str]) -> list[str]:
    messages = {
        "IMAGE_DIMENSIONS_TOO_SMALL": "图片尺寸不足，请使用宽高均不低于 720px 的照片。",
        "PERSON_NOT_FOUND": "未检测到人物，请上传清晰单人照片。",
        "MULTIPLE_PEOPLE": "检测到多个人物，请上传仅包含一人的照片。",
        "FACE_COUNT_INVALID": "未检测到唯一清晰人脸，请更换正脸或近正脸照片。",
        "FACE_NOT_VISIBLE": "人物面部不可见或过小，请更换无遮挡照片。",
        "IMAGE_NOT_SHARP": "照片清晰度不足，请使用对焦清楚的原图。",
        "FACE_OCCLUDED": "人物面部存在明显遮挡，请更换照片。",
        "WATERMARK_DETECTED": "照片包含文字或水印，请上传无水印原图。",
    }
    return [messages[code] for code in issue_codes if code in messages]


def character_not_found(code: str, message: str) -> HTTPException:
    return character_error(404, code, message)


def character_error(
    status_code: int,
    code: str,
    message: str,
    **extra: object,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **extra},
    )
