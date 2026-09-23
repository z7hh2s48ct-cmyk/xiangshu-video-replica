from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.permissions import write_audit

MAIN_CHARACTER_VERSION_KIND = "main_character"
LEGACY_IDENTITY_PREFIX = "legacy-identity:"
LEGACY_PERSONA_PREFIX = "legacy-persona:"
LEGACY_CHARACTER_VERSION_PREFIX = "legacy-version:"
LEGACY_CHARACTER_ASSET_PREFIX = "legacy-asset:"
LEGACY_CHARACTER_TEMPLATE_VERSION = "legacy-character-v1"
LEGACY_CHARACTER_TEMPLATE = (
    "Grandfather an imported legacy character snapshot without claiming five generated views."
)
LEGACY_CHARACTER_TEMPLATE_HASH = hashlib.sha256(LEGACY_CHARACTER_TEMPLATE.encode()).hexdigest()


@dataclass(frozen=True)
class CharacterData:
    id: str
    name: str
    reference_asset_ids: list[str]
    authorization_project_ids: list[str]
    authorization_expires_at: str | None
    is_active: bool
    created_by_user_id: str | None
    created_at: str
    updated_at: str


def list_characters(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str | None,
) -> list[CharacterData]:
    rows = conn.execute(
        """
        SELECT
            id,
            name,
            reference_asset_ids_json,
            authorization_project_ids_json,
            authorization_expires_at,
            is_active,
            created_by_user_id,
            created_at,
            updated_at
        FROM characters
        ORDER BY created_at, id
        """
    ).fetchall()
    characters = [character_from_row(row) for row in rows]
    if actor.role in {"employee", "customer"}:
        return [
            character
            for character in characters
            if character_is_owned_or_project_authorized(
                character,
                actor=actor,
                project_id=project_id,
            )
            and character_is_available(character, project_id=project_id)
        ]
    return characters


def get_character(
    conn: BusinessConnection,
    *,
    character_id: str,
    actor: CurrentUser,
    project_id: str | None,
) -> CharacterData:
    character = read_character(conn, character_id)
    if actor.role in {"employee", "customer"}:
        if not character_is_owned_or_project_authorized(
            character,
            actor=actor,
            project_id=project_id,
        ) or not character_is_available(character, project_id=project_id):
            raise character_not_available()
    return character


def create_character(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    name: str,
    reference_asset_ids: list[str],
    authorization_project_ids: list[str],
    authorization_expires_at: datetime | None,
    is_active: bool,
) -> CharacterData:
    clean_name = normalize_name(name)
    clean_reference_asset_ids = normalize_ids(reference_asset_ids)
    clean_authorization_project_ids = normalize_ids(authorization_project_ids)
    ensure_assets_exist(conn, clean_reference_asset_ids)
    ensure_projects_exist(conn, clean_authorization_project_ids)

    character_id = str(uuid4())
    expires_at = encode_datetime(authorization_expires_at)
    with conn:
        conn.execute(
            """
            INSERT INTO characters (
                id,
                name,
                reference_asset_ids_json,
                authorization_project_ids_json,
                authorization_expires_at,
                is_active,
                created_by_user_id
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                character_id,
                clean_name,
                encode_json_list(clean_reference_asset_ids),
                encode_json_list(clean_authorization_project_ids),
                expires_at,
                1 if is_active else 0,
                actor.id,
            ),
        )
        character = read_character(conn, character_id)
        sync_legacy_character_domain(conn, actor=actor, character=character)
    write_audit(
        conn,
        actor=actor,
        action="character.create",
        entity_type="character",
        entity_id=character_id,
    )
    return character


def update_character(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    character_id: str,
    name: str | None,
    reference_asset_ids: list[str] | None,
    authorization_project_ids: list[str] | None,
    authorization_expires_at: datetime | None,
    clear_authorization_expires_at: bool,
    is_active: bool | None,
) -> CharacterData:
    read_character(conn, character_id)
    updates: list[str] = []
    params: list[object] = []

    if name is not None:
        updates.append("name = %s")
        params.append(normalize_name(name))
    if reference_asset_ids is not None:
        clean_reference_asset_ids = normalize_ids(reference_asset_ids)
        ensure_assets_exist(conn, clean_reference_asset_ids)
        updates.append("reference_asset_ids_json = %s")
        params.append(encode_json_list(clean_reference_asset_ids))
    if authorization_project_ids is not None:
        clean_project_ids = normalize_ids(authorization_project_ids)
        ensure_projects_exist(conn, clean_project_ids)
        updates.append("authorization_project_ids_json = %s")
        params.append(encode_json_list(clean_project_ids))
    if clear_authorization_expires_at:
        updates.append("authorization_expires_at = NULL")
    elif authorization_expires_at is not None:
        updates.append("authorization_expires_at = %s")
        params.append(encode_datetime(authorization_expires_at))
    if is_active is not None:
        updates.append("is_active = %s")
        params.append(1 if is_active else 0)

    updated_fields = [assignment.split(" =", 1)[0] for assignment in updates]
    if updates:
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(character_id)
        with conn:
            conn.execute(
                f"""
                UPDATE characters
                SET {", ".join(updates)}
                WHERE id = %s
                """,
                params,
            )
            character = read_character(conn, character_id)
            sync_legacy_character_domain(conn, actor=actor, character=character)
    else:
        character = read_character(conn, character_id)
    write_audit(
        conn,
        actor=actor,
        action="character.update",
        entity_type="character",
        entity_id=character_id,
        metadata={"updated_fields": updated_fields},
    )
    return character


def delete_character(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    character_id: str,
) -> None:
    read_character(conn, character_id)
    with conn:
        conn.execute(
            """
            UPDATE person_identities
            SET status = 'ARCHIVED', updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (f"{LEGACY_IDENTITY_PREFIX}{character_id}",),
        )
        conn.execute("DELETE FROM characters WHERE id = %s", (character_id,))
    write_audit(
        conn,
        actor=actor,
        action="character.delete",
        entity_type="character",
        entity_id=character_id,
    )


def choose_project_main_character(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    character_id: str,
) -> dict[str, object]:
    character = get_character(
        conn,
        character_id=character_id,
        actor=actor,
        project_id=project_id,
    )
    if not character_is_available(character, project_id=project_id):
        raise character_not_available()

    snapshot = character_snapshot(character)
    compatibility_snapshot_json = json.dumps(
        snapshot,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    version_number = next_version_number(conn, project_id)
    version_id = str(uuid4())
    payload = {
        "project_id": project_id,
        "character_id": character.id,
        "character_snapshot": snapshot,
        "selected_by_user_id": actor.id,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO versions (
                id,
                project_id,
                asset_id,
                kind,
                version_number,
                payload_json,
                created_by_user_id
            )
            VALUES (%s, %s, NULL, %s, %s, %s, %s)
            """,
            (
                version_id,
                project_id,
                MAIN_CHARACTER_VERSION_KIND,
                version_number,
                json.dumps(payload, ensure_ascii=True, sort_keys=True),
                actor.id,
            ),
        )
        conn.execute(
            """
            INSERT INTO project_main_characters (
                project_id,
                character_id,
                version_id,
                character_version_id,
                selected_by_user_id
            )
            VALUES (
                %s,
                %s,
                %s,
                (
                    SELECT id
                    FROM character_versions AS compatibility_version
                    WHERE compatibility_version.persona_id = %s
                      AND compatibility_version.status = 'PUBLISHED'
                      AND compatibility_version.persona_snapshot_json = %s
                    ORDER BY compatibility_version.version_number DESC
                    LIMIT 1
                ),
                %s
            )
            ON CONFLICT(project_id) DO UPDATE SET
                character_id = excluded.character_id,
                version_id = excluded.version_id,
                character_version_id = excluded.character_version_id,
                selected_by_user_id = excluded.selected_by_user_id,
                selected_at = CURRENT_TIMESTAMP
            """,
            (
                project_id,
                character_id,
                version_id,
                f"{LEGACY_PERSONA_PREFIX}{character_id}",
                compatibility_snapshot_json,
                actor.id,
            ),
        )
    return {
        "project_id": project_id,
        "character_id": character_id,
        "character_version_id": None,
        "version_id": version_id,
        "version_number": version_number,
        "character_snapshot": snapshot,
    }


def get_project_main_character(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT
            main_character.version_id,
            version.version_number,
            version.payload_json
        FROM project_main_characters AS main_character
        JOIN versions AS version ON version.id = main_character.version_id
        WHERE main_character.project_id = %s
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "MAIN_CHARACTER_NOT_FOUND",
                "message": "Project has no main character.",
            },
        )
    payload = json.loads(str(row["payload_json"]))
    return {
        "project_id": project_id,
        "character_id": (
            None if payload.get("character_id") is None else str(payload["character_id"])
        ),
        "character_version_id": (
            None
            if payload.get("character_version_id") is None
            else str(payload["character_version_id"])
        ),
        "version_id": str(row["version_id"]),
        "version_number": int(row["version_number"]),
        "character_snapshot": payload["character_snapshot"],
    }


def sync_legacy_character_domain(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    character: CharacterData,
) -> str:
    identity_id = f"{LEGACY_IDENTITY_PREFIX}{character.id}"
    persona_id = f"{LEGACY_PERSONA_PREFIX}{character.id}"
    status, authorization_status = legacy_identity_status(character)
    scope_json = encode_json_list(character.authorization_project_ids)
    source_asset_id = character.reference_asset_ids[0] if character.reference_asset_ids else None
    source_sha256 = asset_sha256(conn, source_asset_id)
    owner_user_id = legacy_character_owner_user_id(conn, character=character, actor=actor)
    snapshot_json = json.dumps(
        character_snapshot(character),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity_exists = conn.execute(
        "SELECT 1 FROM person_identities WHERE id = %s",
        (identity_id,),
    ).fetchone()
    if identity_exists is None:
        conn.execute(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                authorization_scope, authorization_expires_at, source_asset_id,
                source_quality_status, status, created_by
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'IMPORTED', %s, %s)
            """,
            (
                identity_id,
                owner_user_id,
                character.name,
                authorization_status,
                scope_json,
                character.authorization_expires_at,
                source_asset_id,
                status,
                actor.id,
            ),
        )
        conn.execute(
            """
            INSERT INTO character_personas (
                id, identity_id, name, appearance_constraints_json,
                usage_scope_json, created_by
            )
            VALUES (%s, %s, %s, '{}', %s, %s)
            """,
            (persona_id, identity_id, character.name, scope_json, actor.id),
        )
    else:
        conn.execute(
            """
            UPDATE person_identities
            SET owner_user_id = %s, display_name = %s, authorization_status = %s,
                authorization_scope = %s,
                authorization_expires_at = %s, source_asset_id = %s,
                source_quality_status = 'IMPORTED', status = %s,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (
                owner_user_id,
                character.name,
                authorization_status,
                scope_json,
                character.authorization_expires_at,
                source_asset_id,
                status,
                identity_id,
            ),
        )
        conn.execute(
            """
            UPDATE character_personas
            SET name = %s, usage_scope_json = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (character.name, scope_json, persona_id),
        )

    version_number = next_legacy_character_version_number(conn, persona_id)
    version_id = legacy_character_version_id(character.id, version_number)
    conn.execute(
        """
        INSERT INTO character_versions (
            id, persona_id, version_number, status, source_asset_id,
            source_sha256, persona_snapshot_json, provider, model,
            generation_params_json, template_version, template_hash,
            required_view_types_json,
            published_by, published_at, created_by
        )
        VALUES (
            %s, %s, %s, 'PUBLISHED', %s, %s, %s, 'legacy-write-through',
            'legacy-character-v1', '{}', %s, %s, '[]', %s, CURRENT_TIMESTAMP, %s
        )
        """,
        (
            version_id,
            persona_id,
            version_number,
            source_asset_id,
            source_sha256,
            snapshot_json,
            LEGACY_CHARACTER_TEMPLATE_VERSION,
            LEGACY_CHARACTER_TEMPLATE_HASH,
            actor.id,
            actor.id,
        ),
    )
    for candidate_number, asset_id in enumerate(character.reference_asset_ids, start=1):
        conn.execute(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type,
                candidate_number, auto_quality_json, review_status,
                is_published_selection
            )
            VALUES (%s, %s, %s, 'IMPORTED_REFERENCE', %s, '{}', 'APPROVED', %s)
            """,
            (
                legacy_character_asset_id(character.id, version_number, candidate_number),
                version_id,
                asset_id,
                candidate_number,
                1 if candidate_number == 1 else 0,
            ),
        )
    return version_id


def next_legacy_character_version_number(conn: BusinessConnection, persona_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM character_versions
        WHERE persona_id = %s
        """,
        (persona_id,),
    ).fetchone()
    return int(row[0])


def legacy_character_version_id(character_id: str, version_number: int) -> str:
    base = f"{LEGACY_CHARACTER_VERSION_PREFIX}{character_id}"
    return base if version_number == 1 else f"{base}:{version_number}"


def legacy_character_asset_id(
    character_id: str,
    version_number: int,
    candidate_number: int,
) -> str:
    base = f"{LEGACY_CHARACTER_ASSET_PREFIX}{character_id}"
    if version_number == 1:
        return f"{base}:{candidate_number}"
    return f"{base}:{version_number}:{candidate_number}"


def legacy_identity_status(character: CharacterData) -> tuple[str, str]:
    if not character.is_active:
        return "REVOKED", "REVOKED"
    if is_expired(character.authorization_expires_at):
        return "EXPIRED", "EXPIRED"
    return "ACTIVE", "AUTHORIZED"


def asset_sha256(conn: BusinessConnection, asset_id: str | None) -> str | None:
    if asset_id is None:
        return None
    row = conn.execute("SELECT sha256 FROM assets WHERE id = %s", (asset_id,)).fetchone()
    return None if row is None else str(row[0])


def read_character(conn: BusinessConnection, character_id: str) -> CharacterData:
    row = conn.execute(
        """
        SELECT
            id,
            name,
            reference_asset_ids_json,
            authorization_project_ids_json,
            authorization_expires_at,
            is_active,
            created_by_user_id,
            created_at,
            updated_at
        FROM characters
        WHERE id = %s
        """,
        (character_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "CHARACTER_NOT_FOUND", "message": "Character does not exist."},
        )
    return character_from_row(row)


def character_from_row(row: sqlite3.Row) -> CharacterData:
    return CharacterData(
        id=str(row["id"]),
        name=str(row["name"]),
        reference_asset_ids=decode_json_list(row["reference_asset_ids_json"]),
        authorization_project_ids=decode_json_list(row["authorization_project_ids_json"]),
        authorization_expires_at=(
            None
            if row["authorization_expires_at"] is None
            else str(row["authorization_expires_at"])
        ),
        is_active=int(row["is_active"]) == 1,
        created_by_user_id=(
            None if row["created_by_user_id"] is None else str(row["created_by_user_id"])
        ),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def character_snapshot(character: CharacterData) -> dict[str, object]:
    return {
        "id": character.id,
        "name": character.name,
        "reference_asset_ids": character.reference_asset_ids,
        "authorization_project_ids": character.authorization_project_ids,
        "authorization_expires_at": character.authorization_expires_at,
        "is_active": character.is_active,
    }


def character_is_available(character: CharacterData, *, project_id: str | None) -> bool:
    if not character.is_active:
        return False
    if is_expired(character.authorization_expires_at):
        return False
    if not character.authorization_project_ids:
        return True
    return project_id in character.authorization_project_ids


def character_is_owned_or_project_authorized(
    character: CharacterData,
    *,
    actor: CurrentUser,
    project_id: str | None,
) -> bool:
    if character.created_by_user_id == actor.id:
        return True
    return project_id is not None and project_id in character.authorization_project_ids


def legacy_character_owner_user_id(
    conn: BusinessConnection,
    *,
    character: CharacterData,
    actor: CurrentUser,
) -> str:
    if not character.authorization_project_ids:
        return actor.id
    placeholders = ", ".join("%s" for _ in character.authorization_project_ids)
    rows = conn.execute(
        f"SELECT DISTINCT owner_user_id FROM projects WHERE id IN ({placeholders})",
        tuple(character.authorization_project_ids),
    ).fetchall()
    owners = {str(row[0]) for row in rows}
    return next(iter(owners)) if len(owners) == 1 else actor.id


def next_version_number(conn: BusinessConnection, project_id: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(MAX(version_number), 0) + 1
        FROM versions
        WHERE project_id = %s AND kind = %s
        """,
        (project_id, MAIN_CHARACTER_VERSION_KIND),
    ).fetchone()
    return int(row[0])


def normalize_name(name: str) -> str:
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(
            status_code=422,
            detail={"code": "CHARACTER_NAME_REQUIRED", "message": "Character name is required."},
        )
    return clean_name


def normalize_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean_value = value.strip()
        if not clean_value or clean_value in seen:
            continue
        seen.add(clean_value)
        result.append(clean_value)
    return result


def ensure_assets_exist(conn: BusinessConnection, asset_ids: list[str]) -> None:
    ensure_ids_exist(conn, table="assets", ids=asset_ids, code="ASSET_NOT_FOUND")


def ensure_projects_exist(conn: BusinessConnection, project_ids: list[str]) -> None:
    ensure_ids_exist(conn, table="projects", ids=project_ids, code="PROJECT_NOT_FOUND")


def ensure_ids_exist(
    conn: BusinessConnection,
    *,
    table: str,
    ids: list[str],
    code: str,
) -> None:
    if not ids:
        return
    placeholders = ", ".join("%s" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE id IN ({placeholders})",
        ids,
    ).fetchall()
    found = {str(row["id"]) for row in rows}
    missing = [item_id for item_id in ids if item_id not in found]
    if missing:
        raise HTTPException(
            status_code=422,
            detail={"code": code, "message": f"Missing ids: {', '.join(missing)}"},
        )


def encode_json_list(values: list[str]) -> str:
    return json.dumps(values, ensure_ascii=True)


def decode_json_list(value: Any) -> list[str]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list):
        return []
    return [str(item) for item in decoded]


def encode_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def is_expired(value: str | None) -> bool:
    if value is None:
        return False
    return parse_datetime(value) <= datetime.now(UTC)


def character_not_available() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "CHARACTER_NOT_AVAILABLE",
            "message": "Character is inactive, expired, or outside this project scope.",
        },
    )
