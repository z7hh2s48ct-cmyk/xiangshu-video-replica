from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal, Never, cast
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser, Role
from app.character_policy import identity_values_are_current
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.ops_metrics import current_request_context, set_current_result_code

logger = logging.getLogger(__name__)
ASSET_FORBIDDEN_MESSAGE = (
    "Employee access is limited to published assets with current portrait authorization."
)


@dataclass(frozen=True)
class SecurityDenialAudit:
    id: str
    actor_user_id: str
    action: str
    entity_type: str
    entity_id: str
    metadata_json: str


class AuditedSecurityDenial(HTTPException):
    """A PG denial whose audit must commit after the business rollback."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        audit: SecurityDenialAudit,
        status_code: int = 403,
        detail: Any | None = None,
    ) -> None:
        set_current_result_code(code)
        super().__init__(
            status_code=status_code,
            detail=detail if detail is not None else {"code": code, "message": message},
        )
        self.audit = audit


def remap_security_denial(
    error: HTTPException,
    *,
    status_code: int,
    detail: dict[str, str],
) -> HTTPException:
    """Change a denial's public resource shape without dropping its PG audit fact."""
    if isinstance(error, AuditedSecurityDenial):
        return AuditedSecurityDenial(
            code=detail["code"],
            message=detail.get("message", ""),
            audit=error.audit,
            status_code=status_code,
            detail=detail,
        )
    return HTTPException(status_code=status_code, detail=detail)


def persist_security_denial(error: AuditedSecurityDenial) -> None:
    """Commit a denial fact independently without changing the public 403.

    Transaction owners call this only after their failed business transaction
    has unwound. The preallocated id makes repeated dependency teardown safe.
    """
    fact = error.audit
    try:
        with pg_transaction() as conn:
            conn.execute(
                "INSERT INTO audit_logs "
                "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (
                    fact.id,
                    fact.actor_user_id,
                    fact.action,
                    fact.entity_type,
                    fact.entity_id,
                    fact.metadata_json,
                ),
            )
    except Exception as audit_error:
        logger.warning(
            "security denial audit unavailable (%s)",
            type(audit_error).__name__,
        )


def _raise_denial_with_audit(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    audit_action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any],
    code: str,
    message: str,
    status_code: int = 403,
) -> Never:
    if conn.is_postgres:
        raise AuditedSecurityDenial(
            code=code,
            message=message,
            status_code=status_code,
            audit=SecurityDenialAudit(
                id=str(uuid4()),
                actor_user_id=actor.id,
                action=audit_action,
                entity_type=entity_type,
                entity_id=entity_id,
                metadata_json=json.dumps(metadata, ensure_ascii=True, sort_keys=True),
            ),
        )
    write_audit(
        conn,
        actor=actor,
        action=audit_action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata,
    )
    raise HTTPException(status_code=status_code, detail={"code": code, "message": message})


def insert_audit(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            actor.id,
            action,
            entity_type,
            entity_id,
            json.dumps(metadata or {}, ensure_ascii=True, sort_keys=True),
        ),
    )


def write_audit(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> None:
    insert_audit(
        conn,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        metadata=metadata,
    )
    if commit:
        conn.commit()


def require_role(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    allowed_roles: set[Role],
    action: str,
    entity_type: str,
    entity_id: str,
) -> None:
    if actor.role in allowed_roles:
        return

    _raise_denial_with_audit(
        conn,
        actor=actor,
        audit_action="security.role_denied",
        entity_type=entity_type,
        entity_id=entity_id,
        metadata={"attempted_action": action, "required_roles": sorted(allowed_roles)},
        code="ROLE_FORBIDDEN",
        message=f"{actor.role} is not allowed to perform {action}.",
    )


def require_not_auditor(
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
        allowed_roles={"employee", "admin", "customer"},
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
    )


def require_project_access(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    action: str,
    evidence_type: Literal["project", "asset"] = "project",
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, owner_user_id, name, status
        FROM projects
        WHERE id = %s
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PROJECT_NOT_FOUND", "message": "Project does not exist."},
        )

    if actor.role in {"admin", "auditor"}:
        return cast(sqlite3.Row, row)

    owner_user_id = str(row["owner_user_id"])
    if conn.is_postgres:
        _record_authorization_evidence(
            conn,
            actor_user_id=actor.id,
            owner_user_id=owner_user_id,
            resource_type=evidence_type,
        )
    if owner_user_id == actor.id:
        return cast(sqlite3.Row, row)

    _raise_denial_with_audit(
        conn,
        actor=actor,
        audit_action="security.project_denied",
        entity_type="project",
        entity_id=project_id,
        metadata={"attempted_action": action},
        code="PROJECT_NOT_FOUND",
        message="Project does not exist.",
        status_code=404,
    )


def require_asset_access(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    asset_id: str,
    action: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, project_id, kind, storage_uri, sha256, size_bytes, content_type,
               metadata_json, created_by_user_id
        FROM assets
        WHERE id = %s
        """,
        (asset_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ASSET_NOT_FOUND", "message": "Asset does not exist."},
        )

    if row["project_id"] is not None:
        try:
            require_project_access(
                conn,
                actor=actor,
                project_id=str(row["project_id"]),
                action=action,
                evidence_type="asset",
            )
        except AuditedSecurityDenial as exc:
            raise AuditedSecurityDenial(
                code="ASSET_NOT_FOUND",
                message="Asset does not exist.",
                audit=exc.audit,
                status_code=404,
            ) from exc
        except HTTPException as exc:
            if exc.status_code == 404:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "ASSET_NOT_FOUND", "message": "Asset does not exist."},
                ) from exc
            raise
        return cast(sqlite3.Row, row)

    if actor.role in {"admin", "auditor"}:
        return cast(sqlite3.Row, row)

    if (
        str(row["kind"]) in {"material_image", "material_audio", "material_video"}
        and row["created_by_user_id"] is not None
        and str(row["created_by_user_id"]) == actor.id
    ):
        return cast(sqlite3.Row, row)

    if (
        str(row["kind"]) == "oral_audio"
        and row["created_by_user_id"] is not None
        and str(row["created_by_user_id"]) == actor.id
    ):
        return cast(sqlite3.Row, row)

    if str(row["kind"]) == "oral_video":
        owned_oral_result = conn.execute(
            """
            SELECT 1
            FROM oral_tasks
            WHERE result_asset_id = %s AND owner_user_id = %s
            LIMIT 1
            """,
            (asset_id, actor.id),
        ).fetchone()
        if owned_oral_result is not None:
            return cast(sqlite3.Row, row)

    # Contact sheets live outside character_assets, so grant access through the
    # published character version referenced in their asset metadata.
    if str(row["kind"]) == "character_contact_sheet":
        sheet_identity = _published_contact_sheet_identity(conn, row)
        if (
            sheet_identity is not None
            and identity_owned_by_actor(sheet_identity, actor)
            and character_identity_is_current(sheet_identity)
        ):
            return cast(sqlite3.Row, row)

    # Simple-upload source photos also live outside character_assets; grant
    # access through the identity recorded in their metadata so first-frame
    # generation can use the uploaded photo as the authoritative face input.
    if str(row["kind"]) == "character_source_image":
        source_identity = _identity_from_asset_metadata(conn, row)
        if (
            source_identity is not None
            and identity_owned_by_actor(source_identity, actor)
            and character_identity_is_current(source_identity)
        ):
            return cast(sqlite3.Row, row)

    published_character = conn.execute(
        """
        SELECT
            identity.owner_user_id,
            identity.authorization_status,
            identity.authorization_expires_at,
            identity.source_quality_status,
            identity.status AS identity_status
        FROM character_assets AS character_asset
        JOIN character_versions AS version
          ON version.id = character_asset.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        WHERE character_asset.asset_id = %s
          AND character_asset.review_status = 'APPROVED'
          AND character_asset.is_published_selection = 1
          AND version.status = 'PUBLISHED'
        LIMIT 1
        """,
        (asset_id,),
    ).fetchone()
    if (
        published_character is not None
        and identity_owned_by_actor(published_character, actor)
        and character_identity_is_current(published_character)
    ):
        return cast(sqlite3.Row, row)

    _raise_denial_with_audit(
        conn,
        actor=actor,
        audit_action="security.asset_denied",
        entity_type="asset",
        entity_id=asset_id,
        metadata={"attempted_action": action},
        code="ASSET_NOT_FOUND",
        message="Asset does not exist.",
        status_code=404,
    )


def character_identity_is_current(row: sqlite3.Row) -> bool:
    return identity_values_are_current(
        status=row["identity_status"],
        authorization_status=row["authorization_status"],
        authorization_expires_at=row["authorization_expires_at"],
        source_quality_status=row["source_quality_status"],
    )


def identity_owned_by_actor(row: sqlite3.Row, actor: CurrentUser) -> bool:
    owner_user_id = row["owner_user_id"]
    return owner_user_id is not None and str(owner_user_id) == actor.id


def _record_authorization_evidence(
    conn: BusinessConnection,
    *,
    actor_user_id: str,
    owner_user_id: str,
    resource_type: Literal["project", "asset"],
) -> None:
    """Write a bounded digest pair in the protected operation's transaction.

    Correct denials roll this candidate row back. If an ownership predicate
    regresses and a mismatch is allowed to commit, the cluster probe retains
    an append-only P1 fact without storing either user identifier.
    """
    context = current_request_context()
    request_id = context.request_id if context is not None else str(uuid4())
    conn.execute(
        "INSERT INTO customer_authorization_evidence "
        "(id, request_id, resource_type, actor_digest, owner_digest) "
        "VALUES (%s, %s, %s, %s, %s) "
        "ON CONFLICT (resource_type, actor_digest, owner_digest) DO NOTHING",
        (
            str(uuid4()),
            request_id,
            resource_type,
            _authorization_digest(actor_user_id),
            _authorization_digest(owner_user_id),
        ),
    )


def _authorization_digest(user_id: str) -> str:
    return hashlib.sha256(f"t37-authorization:{user_id}".encode()).hexdigest()


def _identity_from_asset_metadata(conn: BusinessConnection, row: sqlite3.Row) -> sqlite3.Row | None:
    """Resolve the identity named in an asset's metadata, if any."""
    try:
        metadata = json.loads(str(row["metadata_json"] or ""))
    except json.JSONDecodeError:
        return None
    identity_id = metadata.get("identity_id") if isinstance(metadata, dict) else None
    if not isinstance(identity_id, str) or not identity_id:
        return None
    identity: sqlite3.Row | None = conn.execute(
        """
        SELECT
            owner_user_id,
            authorization_status,
            authorization_expires_at,
            source_quality_status,
            status AS identity_status
        FROM person_identities
        WHERE id = %s
        """,
        (identity_id,),
    ).fetchone()
    return identity


def _published_contact_sheet_identity(
    conn: BusinessConnection, row: sqlite3.Row
) -> sqlite3.Row | None:
    """Resolve the identity behind a contact sheet whose version is published."""
    try:
        metadata = json.loads(str(row["metadata_json"] or ""))
    except json.JSONDecodeError:
        return None
    version_id = metadata.get("character_version_id") if isinstance(metadata, dict) else None
    if not isinstance(version_id, str) or not version_id:
        return None
    identity: sqlite3.Row | None = conn.execute(
        """
        SELECT
            identity.owner_user_id,
            identity.authorization_status,
            identity.authorization_expires_at,
            identity.source_quality_status,
            identity.status AS identity_status
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        WHERE version.id = %s
          AND version.status = 'PUBLISHED'
        LIMIT 1
        """,
        (version_id,),
    ).fetchone()
    return identity


def project_id_for_task(conn: BusinessConnection, task_id: str) -> str:
    row = conn.execute(
        """
        SELECT generation_batches.project_id
        FROM generation_tasks
        JOIN generation_batches ON generation_batches.id = generation_tasks.batch_id
        WHERE generation_tasks.id = %s
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "TASK_NOT_FOUND", "message": "Generation task does not exist."},
        )
    return str(row["project_id"])


def forbidden(code: str, message: str) -> HTTPException:
    set_current_result_code(code)
    return HTTPException(status_code=403, detail={"code": code, "message": message})
