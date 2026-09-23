from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import defaultdict
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser
from app.character_contracts import (
    ProjectCharacterAssetOption,
    ProjectCharacterVersionOption,
)
from app.character_identity import (
    REQUIRED_CHARACTER_VIEW_TYPES,
    decode_object,
    decode_string_list,
    encode_json,
    parse_datetime,
)
from app.character_policy import identity_values_are_current, scope_allows_project
from app.characters import (
    MAIN_CHARACTER_VERSION_KIND,
    get_project_main_character,
    next_version_number,
)
from app.db_portable import BusinessConnection

logger = logging.getLogger(__name__)

SELECTION_SNAPSHOT_SCHEMA_VERSION = "project-character-selection.v1"


def list_available_project_character_versions(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    character_version_id: str | None = None,
) -> list[ProjectCharacterVersionOption]:
    can_read_all = actor.role in {"admin", "auditor"}
    version_rows = conn.execute(
        """
        SELECT
            version.id AS character_version_id,
            version.version_number,
            version.persona_snapshot_json,
            version.provider,
            version.model,
            version.template_version,
            version.template_hash,
            version.required_view_types_json,
            version.published_at,
            version.publication_snapshot_json,
            version.publication_hash,
            persona.id AS persona_id,
            identity.id AS identity_id,
            identity.display_name AS identity_name,
            identity.authorization_status,
            identity.authorization_asset_id,
            identity.authorization_scope,
            identity.authorization_expires_at,
            identity.source_asset_id,
            identity.source_quality_status,
            identity.status AS identity_status
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        JOIN person_identities AS identity ON identity.id = persona.identity_id
        WHERE version.status = 'PUBLISHED'
          AND (%s::text IS NULL OR version.id = %s)
          AND (%s OR identity.owner_user_id = %s)
        ORDER BY LOWER(identity.display_name), persona.id,
                 version.version_number DESC
        """,
        (character_version_id, character_version_id, can_read_all, actor.id),
    ).fetchall()
    if not version_rows:
        return []

    asset_rows = conn.execute(
        """
        SELECT
            version.id AS character_version_id,
            character_asset.id AS character_asset_id,
            character_asset.asset_id,
            character_asset.view_type,
            asset.storage_uri,
            asset.sha256,
            asset.size_bytes,
            asset.content_type
        FROM character_versions AS version
        JOIN character_assets AS character_asset
          ON character_asset.character_version_id = version.id
        JOIN assets AS asset ON asset.id = character_asset.asset_id
        WHERE version.status = 'PUBLISHED'
          AND character_asset.review_status = 'APPROVED'
          AND character_asset.is_published_selection = 1
          AND (%s::text IS NULL OR version.id = %s)
        """,
        (character_version_id, character_version_id),
    ).fetchall()
    assets_by_version: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in asset_rows:
        assets_by_version[str(row["character_version_id"])].append(row)

    options: list[ProjectCharacterVersionOption] = []
    for row in version_rows:
        if (
            row["authorization_asset_id"] is None
            or row["source_asset_id"] is None
            or not identity_values_are_current(
                status=row["identity_status"],
                authorization_status=row["authorization_status"],
                authorization_expires_at=row["authorization_expires_at"],
                source_quality_status=row["source_quality_status"],
            )
        ):
            continue
        option = available_option_from_rows(
            row,
            assets_by_version.get(str(row["character_version_id"]), []),
            project_id=project_id,
        )
        if option is not None:
            options.append(option)
    return options


def choose_project_character_version(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    character_version_id: str,
) -> dict[str, object]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        options = list_available_project_character_versions(
            conn,
            actor=actor,
            project_id=project_id,
            character_version_id=character_version_id,
        )
        if not options:
            raise unavailable_character_version()
        option = options[0]
        current = conn.execute(
            """
            SELECT binding.character_version_id, version.payload_json
            FROM project_main_characters AS binding
            JOIN versions AS version ON version.id = binding.version_id
            WHERE binding.project_id = %s
            """,
            (project_id,),
        ).fetchone()
        if current is not None and str(current["character_version_id"]) == character_version_id:
            current_snapshot = decode_object(current["payload_json"])
            character_snapshot = current_snapshot.get("character_snapshot")
            if (
                isinstance(character_snapshot, dict)
                and character_snapshot.get("schema_version") == SELECTION_SNAPSHOT_SCHEMA_VERSION
                and character_snapshot.get("character_version_id") == character_version_id
            ):
                result = get_project_main_character(conn, project_id=project_id)
                conn.commit()
                return result

        selection_version_id = str(uuid4())
        selection_version_number = next_version_number(conn, project_id)
        snapshot = project_character_snapshot(option)
        payload = {
            "project_id": project_id,
            "character_id": None,
            "character_version_id": option.character_version_id,
            "character_snapshot": snapshot,
            "selected_by_user_id": actor.id,
        }
        conn.execute(
            """
            INSERT INTO versions (
                id, project_id, asset_id, kind, version_number,
                payload_json, created_by_user_id
            ) VALUES (%s, %s, NULL, %s, %s, %s, %s)
            """,
            (
                selection_version_id,
                project_id,
                MAIN_CHARACTER_VERSION_KIND,
                selection_version_number,
                encode_json(payload),
                actor.id,
            ),
        )
        conn.execute(
            """
            INSERT INTO project_main_characters (
                project_id, character_id, version_id, character_version_id,
                selected_by_user_id
            ) VALUES (%s, NULL, %s, %s, %s)
            ON CONFLICT(project_id) DO UPDATE SET
                character_id = NULL,
                version_id = excluded.version_id,
                character_version_id = excluded.character_version_id,
                selected_by_user_id = excluded.selected_by_user_id,
                selected_at = CURRENT_TIMESTAMP
            """,
            (
                project_id,
                selection_version_id,
                option.character_version_id,
                actor.id,
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (
                id, actor_user_id, action, entity_type, entity_id, metadata_json
            ) VALUES (%s, %s, 'project.main_character.choose_version',
                      'version', %s, %s)
            """,
            (
                str(uuid4()),
                actor.id,
                selection_version_id,
                encode_json(
                    {
                        "character_version_id": option.character_version_id,
                        "project_id": project_id,
                        "publication_hash": option.publication_hash,
                    }
                ),
            ),
        )
        result = get_project_main_character(conn, project_id=project_id)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        logger.exception(
            "Project character version selection failed",
            extra={
                "project_id": project_id,
                "character_version_id": character_version_id,
            },
        )
        raise HTTPException(
            status_code=500,
            detail={"code": "PROJECT_CHARACTER_SELECTION_FAILED"},
        ) from exc
    return result


def available_option_from_rows(
    version: sqlite3.Row,
    asset_rows: list[sqlite3.Row],
    *,
    project_id: str,
) -> ProjectCharacterVersionOption | None:
    try:
        version_id = str(version["character_version_id"])
        required_views = decode_string_list(version["required_view_types_json"])
        if not set(REQUIRED_CHARACTER_VIEW_TYPES).issubset(set(required_views)):
            return None
        persona_snapshot = decode_object(version["persona_snapshot_json"])
        if not scope_allows_project(
            decode_string_list(version["authorization_scope"]),
            project_id=project_id,
        ) or not scope_allows_project(
            persona_snapshot.get("usage_scope_json"),
            project_id=project_id,
        ):
            return None
        publication_json = str(version["publication_snapshot_json"])
        publication_hash = str(version["publication_hash"])
        if hashlib.sha256(publication_json.encode()).hexdigest() != publication_hash:
            return None
        publication = decode_object(publication_json)
        assets_snapshot = publication.get("assets_by_view")
        publication_views = publication.get("required_view_types")
        if (
            publication.get("schema_version") != "character-publication.v1"
            or publication.get("character_version_id") != version_id
            or not (
                isinstance(publication_views, list)
                and set(REQUIRED_CHARACTER_VIEW_TYPES).issubset(
                    {str(view) for view in publication_views}
                )
            )
            or not isinstance(assets_snapshot, dict)
        ):
            return None
        assets_by_view = {str(row["view_type"]): row for row in asset_rows}
        if not set(REQUIRED_CHARACTER_VIEW_TYPES).issubset(set(assets_by_view)):
            return None
        assets: list[ProjectCharacterAssetOption] = []
        for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
            asset = assets_by_view[view_type]
            frozen = assets_snapshot.get(view_type)
            if not isinstance(frozen, dict) or not published_asset_matches(asset, frozen):
                return None
            assets.append(
                ProjectCharacterAssetOption(
                    character_asset_id=str(asset["character_asset_id"]),
                    asset_id=str(asset["asset_id"]),
                    view_type=view_type,
                )
            )
        published_at = version["published_at"]
        if published_at is None:
            return None
        expires_at = version["authorization_expires_at"]
        return ProjectCharacterVersionOption(
            character_version_id=version_id,
            version_number=int(version["version_number"]),
            identity_id=str(version["identity_id"]),
            identity_name=str(version["identity_name"]),
            authorization_expires_at=(
                None if expires_at is None else parse_datetime(str(expires_at))
            ),
            persona_id=str(version["persona_id"]),
            persona_snapshot_json=persona_snapshot,
            provider=None if version["provider"] is None else str(version["provider"]),
            model=None if version["model"] is None else str(version["model"]),
            template_version=(
                None if version["template_version"] is None else str(version["template_version"])
            ),
            template_hash=(
                None if version["template_hash"] is None else str(version["template_hash"])
            ),
            published_at=parse_datetime(str(published_at)),
            publication_hash=publication_hash,
            assets=assets,
        )
    except (KeyError, TypeError, ValueError):
        return None


def published_asset_matches(asset: sqlite3.Row, frozen: dict[str, object]) -> bool:
    return (
        frozen.get("approved_asset_id") == str(asset["asset_id"])
        and frozen.get("character_asset_id") == str(asset["character_asset_id"])
        and frozen.get("storage_uri") == str(asset["storage_uri"])
        and frozen.get("sha256") == str(asset["sha256"])
        and frozen.get("size_bytes") == int(asset["size_bytes"])
        and frozen.get("content_type") == str(asset["content_type"])
    )


def project_character_snapshot(option: ProjectCharacterVersionOption) -> dict[str, object]:
    return {
        "schema_version": SELECTION_SNAPSHOT_SCHEMA_VERSION,
        "character_version_id": option.character_version_id,
        "character_version_number": option.version_number,
        "identity": {
            "id": option.identity_id,
            "display_name": option.identity_name,
            "authorization_expires_at": (
                None
                if option.authorization_expires_at is None
                else option.authorization_expires_at.isoformat()
            ),
        },
        "persona_id": option.persona_id,
        "persona_snapshot_json": option.persona_snapshot_json,
        "provider": option.provider,
        "model": option.model,
        "template_version": option.template_version,
        "template_hash": option.template_hash,
        "published_at": option.published_at.isoformat(),
        "publication_hash": option.publication_hash,
        "published_assets": [asset.model_dump(mode="json") for asset in option.assets],
    }


def unavailable_character_version() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": "CHARACTER_VERSION_NOT_AVAILABLE",
            "message": "Character version is not published or its authorization is unavailable.",
        },
    )
