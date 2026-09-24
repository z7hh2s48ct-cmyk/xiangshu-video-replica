from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Literal, cast
from uuid import uuid4

from fastapi import HTTPException

from app.auth import CurrentUser
from app.character_contracts import (
    CharacterReferenceRecommendation,
    CharacterReferenceSelection,
    ProjectCharacterAssetOption,
    RequiredCharacterViewType,
)
from app.character_identity import (
    REQUIRED_CHARACTER_VIEW_TYPES,
    decode_object,
    encode_json,
    parse_datetime,
)
from app.character_policy import identity_values_are_current
from app.db_portable import BusinessConnection
from app.permissions import require_not_auditor, require_project_access
from app.source_frames import (
    SOURCE_FRAME_CANDIDATES_KIND,
    SOURCE_FRAME_SELECTION_KIND,
    latest_version,
)

logger = logging.getLogger(__name__)

SourceOrientation = Literal["FRONT", "LEFT_45", "RIGHT_45", "LEFT_SIDE", "RIGHT_SIDE"]
SourceShotSize = Literal["CLOSE_UP", "HALF_BODY", "FULL_BODY"]
BodyCompleteness = Literal["FACE_ONLY", "UPPER_BODY", "FULL_BODY", "PARTIAL"]

VALID_ORIENTATIONS = frozenset({"FRONT", "LEFT_45", "RIGHT_45", "LEFT_SIDE", "RIGHT_SIDE"})
VALID_SHOT_SIZES = frozenset({"CLOSE_UP", "HALF_BODY", "FULL_BODY"})
VALID_BODY_COMPLETENESS = frozenset({"FACE_ONLY", "UPPER_BODY", "FULL_BODY", "PARTIAL"})
REFERENCE_SELECTION_SCHEMA_VERSION = "character-reference-selection.v1"
REFERENCE_VERSION_SNAPSHOT_SCHEMA_VERSION = "character-reference-version.v1"


@dataclass(frozen=True)
class SourceFrameFeatures:
    orientation: SourceOrientation
    shot_size: SourceShotSize
    face_visible: bool
    body_completeness: BodyCompleteness


# Auto-matching fallback: the project-detail flow never asks the user to label
# the source frame, so a selection without stored features resolves to these
# conservative defaults (front-facing half-body frame with the face visible).
DEFAULT_SOURCE_FRAME_FEATURES = SourceFrameFeatures(
    orientation="FRONT",
    shot_size="HALF_BODY",
    face_visible=True,
    body_completeness="UPPER_BODY",
)


@dataclass(frozen=True)
class PublishedReferenceAsset:
    character_asset_id: str
    asset_id: str
    view_type: RequiredCharacterViewType
    storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str


@dataclass(frozen=True)
class ReferenceSelectionInputs:
    source_frame_version_id: str
    main_character_version_id: str
    character_version_id: str
    recommended_asset_ids: list[str]
    selected_asset_ids: list[str]
    candidate_assets: list[ProjectCharacterAssetOption]
    recommendation_reason: dict[str, object]
    character_version_snapshot: dict[str, object]


_BODY_VIEW_BY_ORIENTATION: dict[SourceOrientation, RequiredCharacterViewType] = {
    "LEFT_45": "LEFT_45",
    "RIGHT_45": "LEFT_45",
    "LEFT_SIDE": "LEFT_SIDE",
    "RIGHT_SIDE": "LEFT_SIDE",
}


def recommended_body_view(features: SourceFrameFeatures) -> RequiredCharacterViewType:
    if features.orientation == "FRONT":
        if features.shot_size == "FULL_BODY" or features.body_completeness == "FULL_BODY":
            return "FRONT_FULL"
        return "FRONT_HALF"
    # 源帧朝向是原片事实（含 RIGHT_*）；资产合同只有左侧真视图，右向源帧改用
    # 对侧真视图做身份/服装参考，朝向差异由生成模型吸收，不再用镜像伪造右侧资产。
    return _BODY_VIEW_BY_ORIENTATION[features.orientation]


def create_character_reference_selection(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
    expected_source_frame_version_id: str,
    expected_character_version_id: str,
    selected_asset_ids: list[str] | None,
) -> CharacterReferenceSelection:
    require_not_auditor(
        conn,
        actor=actor,
        action="character_reference.select",
        entity_type="project",
        entity_id=project_id,
    )
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="character_reference.select",
    )

    try:
        conn.execute("BEGIN IMMEDIATE")
        inputs = load_reference_selection_inputs(
            conn,
            project_id=project_id,
            requested_asset_ids=selected_asset_ids,
        )
        if (
            inputs.source_frame_version_id != expected_source_frame_version_id
            or inputs.character_version_id != expected_character_version_id
        ):
            raise reference_error(
                409,
                "CHARACTER_REFERENCE_INPUT_BINDING_STALE",
                "源画面或角色版本已变化，请重新查看并确认人物参考图。",
            )
        latest = latest_character_reference_selection_row(conn, project_id=project_id)
        if latest is not None and selection_row_matches(latest, inputs):
            conn.commit()
            return character_reference_selection_from_row(latest)

        selection_id = str(uuid4())
        conn.execute(
            """
            INSERT INTO character_reference_selections (
                id, project_id, source_frame_version_id, character_version_id,
                recommended_asset_ids_json, selected_asset_ids_json,
                recommendation_reason_json, character_version_snapshot_json,
                selected_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                selection_id,
                project_id,
                inputs.source_frame_version_id,
                inputs.character_version_id,
                encode_json(inputs.recommended_asset_ids),
                encode_json(inputs.selected_asset_ids),
                encode_json(inputs.recommendation_reason),
                encode_json(inputs.character_version_snapshot),
                actor.id,
            ),
        )
        insert_reference_audit(
            conn,
            actor=actor,
            selection_id=selection_id,
            project_id=project_id,
            inputs=inputs,
        )
        row = read_character_reference_selection_row(conn, selection_id=selection_id)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        logger.exception("Character reference selection persistence failed")
        raise reference_error(
            500,
            "CHARACTER_REFERENCE_PERSIST_FAILED",
            "人物参考图选择未能保存，请重试。",
        ) from exc
    return character_reference_selection_from_row(row)


def get_latest_character_reference_selection(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
) -> CharacterReferenceSelection:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="character_reference.read",
    )
    row = latest_character_reference_selection_row(conn, project_id=project_id)
    if row is None:
        raise reference_error(
            404,
            "CHARACTER_REFERENCE_SELECTION_NOT_FOUND",
            "项目尚未选择人物参考图。",
        )
    return character_reference_selection_from_row(row)


def get_character_reference_recommendation(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str,
) -> CharacterReferenceRecommendation:
    require_project_access(
        conn,
        actor=actor,
        project_id=project_id,
        action="character_reference.read",
    )
    inputs = load_reference_selection_inputs(
        conn,
        project_id=project_id,
        requested_asset_ids=None,
    )
    return CharacterReferenceRecommendation(
        source_frame_version_id=inputs.source_frame_version_id,
        character_version_id=inputs.character_version_id,
        recommended_asset_ids_json=inputs.recommended_asset_ids,
        candidate_assets=inputs.candidate_assets,
        recommendation_reason_json=inputs.recommendation_reason,
        character_version_snapshot_json=inputs.character_version_snapshot,
    )


def current_character_reference_selection_for_generation(
    conn: BusinessConnection,
    *,
    project_id: str,
    source_frame_version_id: str,
    expected_selection_id: str | None = None,
    require_usable_character: bool = True,
) -> CharacterReferenceSelection | None:
    row = latest_character_reference_selection_row(conn, project_id=project_id)
    if row is None:
        if expected_selection_id is None:
            return None
        raise stale_reference_selection()
    try:
        selection = character_reference_selection_from_row(row)
        binding = current_main_character_binding(conn, project_id=project_id)
        snapshot = selection.character_version_snapshot_json
        version, _, identity = load_character_context(conn, selection.character_version_id)
        publication_snapshot, publication_hash = validated_publication(version)
        published_assets = load_published_reference_assets(
            conn,
            version_id=selection.character_version_id,
            publication_snapshot=publication_snapshot,
        )
        published_ids = {asset.asset_id for asset in published_assets.values()}
        if (
            (expected_selection_id is not None and selection.id != expected_selection_id)
            or selection.source_frame_version_id != source_frame_version_id
            or selection.character_version_id != str(binding["character_version_id"])
            or snapshot.get("main_character_version_id") != str(binding["version_id"])
            or snapshot.get("character_version_id") != selection.character_version_id
            or snapshot.get("persona_id") != str(version["persona_id"])
            or snapshot.get("version_number") != int(version["version_number"])
            or snapshot.get("persona_snapshot_json")
            != decode_object(version["persona_snapshot_json"])
            or snapshot.get("publication_hash") != publication_hash
            or snapshot.get("publication_snapshot_json") != publication_snapshot
            or not 1 <= len(selection.selected_asset_ids_json) <= 4
            or len(set(selection.selected_asset_ids_json)) != len(selection.selected_asset_ids_json)
            or any(asset_id not in published_ids for asset_id in selection.selected_asset_ids_json)
            or (
                require_usable_character
                and (
                    str(version["status"]) != "PUBLISHED"
                    or not identity_values_are_current(
                        status=identity["status"],
                        authorization_status=identity["authorization_status"],
                        authorization_expires_at=identity["authorization_expires_at"],
                        source_quality_status=identity["source_quality_status"],
                    )
                )
            )
        ):
            raise stale_reference_selection()
    except HTTPException as exc:
        if (
            isinstance(exc.detail, dict)
            and exc.detail.get("code") == "CHARACTER_REFERENCE_SELECTION_STALE"
        ):
            raise
        raise stale_reference_selection() from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise stale_reference_selection() from exc
    return selection


def load_reference_selection_inputs(
    conn: BusinessConnection,
    *,
    project_id: str,
    requested_asset_ids: list[str] | None,
) -> ReferenceSelectionInputs:
    source_selection = current_source_selection(conn, project_id=project_id)
    source_features = parse_source_frame_features(source_selection)
    main_character = current_main_character_binding(conn, project_id=project_id)
    character_version_id = str(main_character["character_version_id"])
    version, persona, identity = load_character_context(conn, character_version_id)
    if str(version["status"]) != "PUBLISHED":
        raise reference_error(
            409,
            "CHARACTER_VERSION_NOT_PUBLISHED",
            "项目当前人物版本尚未发布。",
        )
    if not identity_values_are_current(
        status=identity["status"],
        authorization_status=identity["authorization_status"],
        authorization_expires_at=identity["authorization_expires_at"],
        source_quality_status=identity["source_quality_status"],
    ):
        raise reference_error(
            409,
            "CHARACTER_IDENTITY_NOT_ACTIVE",
            "项目当前人物的肖像授权或真人源图已失效。",
        )

    publication_snapshot, publication_hash = validated_publication(version)
    assets_by_view = load_published_reference_assets(
        conn,
        version_id=character_version_id,
        publication_snapshot=publication_snapshot,
    )
    candidate_asset_ids = [assets_by_view[view].asset_id for view in REQUIRED_CHARACTER_VIEW_TYPES]
    candidate_assets = [
        ProjectCharacterAssetOption(
            character_asset_id=assets_by_view[view].character_asset_id,
            asset_id=assets_by_view[view].asset_id,
            view_type=view,
        )
        for view in REQUIRED_CHARACTER_VIEW_TYPES
    ]
    body_view = recommended_body_view(source_features)
    recommended_views: list[RequiredCharacterViewType] = [body_view, "FRONT_FACE"]
    recommended_asset_ids = unique_ids(
        [assets_by_view[view].asset_id for view in recommended_views]
    )
    selected = (
        recommended_asset_ids
        if requested_asset_ids is None
        else normalize_selected_asset_ids(requested_asset_ids)
    )
    if any(asset_id not in candidate_asset_ids for asset_id in selected):
        raise reference_error(
            422,
            "CHARACTER_REFERENCE_ASSET_INVALID",
            "只能选择当前已发布五视图中的人物参考图。",
        )

    source_frame_version_id = str(source_selection["id"])
    main_character_version_id = str(main_character["version_id"])
    persona_snapshot = decode_object(version["persona_snapshot_json"])
    character_version_snapshot: dict[str, object] = {
        "schema_version": REFERENCE_VERSION_SNAPSHOT_SCHEMA_VERSION,
        "character_version_id": character_version_id,
        "persona_id": str(persona["id"]),
        "version_number": int(version["version_number"]),
        "main_character_version_id": main_character_version_id,
        "persona_snapshot_json": persona_snapshot,
        "publication_hash": publication_hash,
        "publication_snapshot_json": publication_snapshot,
    }
    recommendation_reason: dict[str, object] = {
        "schema_version": REFERENCE_SELECTION_SCHEMA_VERSION,
        "source_frame_features": {
            "orientation": source_features.orientation,
            "shot_size": source_features.shot_size,
            "face_visible": source_features.face_visible,
            "body_completeness": source_features.body_completeness,
        },
        "body_view_type": body_view,
        "recommended_view_types": recommended_views,
        "candidate_asset_ids": candidate_asset_ids,
        "rules": ["MATCH_BODY_ORIENTATION_AND_CROP", "ALWAYS_INCLUDE_FRONT_FACE"],
    }
    return ReferenceSelectionInputs(
        source_frame_version_id=source_frame_version_id,
        main_character_version_id=main_character_version_id,
        character_version_id=character_version_id,
        recommended_asset_ids=recommended_asset_ids,
        selected_asset_ids=selected,
        candidate_assets=candidate_assets,
        recommendation_reason=recommendation_reason,
        character_version_snapshot=character_version_snapshot,
    )


def current_source_selection(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> dict[str, object]:
    selection = latest_version(conn, project_id, SOURCE_FRAME_SELECTION_KIND)
    candidates = latest_version(conn, project_id, SOURCE_FRAME_CANDIDATES_KIND)
    if selection is None or candidates is None:
        raise reference_error(
            409,
            "SOURCE_FRAME_SELECTION_REQUIRED",
            "请先确认源画面。",
        )
    payload = decode_object(selection["payload_json"])
    if payload.get("source_frame_candidates_version_id") != str(candidates["id"]):
        raise reference_error(
            409,
            "SOURCE_FRAME_SELECTION_STALE",
            "请从最新候选中重新确认源画面。",
        )
    return payload | {"id": str(selection["id"])}


def parse_source_frame_features(selection: dict[str, object]) -> SourceFrameFeatures:
    """Parse the confirmed source frame's character features.

    Missing or partially invalid features fall back to
    ``DEFAULT_SOURCE_FRAME_FEATURES`` instead of blocking auto-matching: the
    project-detail flow selects the frame automatically and never collects a
    feature form, while legacy selections predating the form simply keep
    working with the conservative defaults.
    """
    raw = selection.get("character_features")
    if not isinstance(raw, dict):
        return DEFAULT_SOURCE_FRAME_FEATURES
    orientation = raw.get("orientation")
    shot_size = raw.get("shot_size")
    face_visible = raw.get("face_visible")
    body_completeness = raw.get("body_completeness")
    if (
        orientation not in VALID_ORIENTATIONS
        or shot_size not in VALID_SHOT_SIZES
        or not isinstance(face_visible, bool)
        or body_completeness not in VALID_BODY_COMPLETENESS
    ):
        return DEFAULT_SOURCE_FRAME_FEATURES
    return SourceFrameFeatures(
        orientation=cast(SourceOrientation, orientation),
        shot_size=cast(SourceShotSize, shot_size),
        face_visible=face_visible,
        body_completeness=cast(BodyCompleteness, body_completeness),
    )


def current_main_character_binding(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT project_id, version_id, character_version_id
        FROM project_main_characters
        WHERE project_id = %s
        """,
        (project_id,),
    ).fetchone()
    if row is None or row["version_id"] is None or row["character_version_id"] is None:
        raise reference_error(
            409,
            "CHARACTER_VERSION_REQUIRED",
            "请先为项目选择一个已发布人物版本。",
        )
    return cast(sqlite3.Row, row)


def load_character_context(
    conn: BusinessConnection,
    version_id: str,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    version = conn.execute(
        "SELECT * FROM character_versions WHERE id = %s",
        (version_id,),
    ).fetchone()
    if version is None:
        raise reference_error(409, "CHARACTER_VERSION_REQUIRED", "项目人物版本不存在。")
    persona = conn.execute(
        "SELECT * FROM character_personas WHERE id = %s",
        (str(version["persona_id"]),),
    ).fetchone()
    if persona is None:
        raise reference_error(409, "CHARACTER_PERSONA_INVALID", "项目人物人设不存在。")
    identity = conn.execute(
        "SELECT * FROM person_identities WHERE id = %s",
        (str(persona["identity_id"]),),
    ).fetchone()
    if identity is None:
        raise reference_error(409, "CHARACTER_IDENTITY_INVALID", "项目人物身份不存在。")
    return cast(sqlite3.Row, version), cast(sqlite3.Row, persona), cast(sqlite3.Row, identity)


def validated_publication(version: sqlite3.Row) -> tuple[dict[str, object], str]:
    snapshot_value = version["publication_snapshot_json"]
    publication_hash_value = version["publication_hash"]
    if snapshot_value is None or publication_hash_value is None:
        raise invalid_publication()
    snapshot = decode_object(snapshot_value)
    publication_hash = str(publication_hash_value)
    expected_hash = hashlib.sha256(encode_json(snapshot).encode()).hexdigest()
    snapshot_views = snapshot.get("required_view_types")
    views_compatible = isinstance(snapshot_views, list) and set(
        REQUIRED_CHARACTER_VIEW_TYPES
    ).issubset({str(view) for view in snapshot_views})
    if (
        publication_hash != expected_hash
        or snapshot.get("schema_version") != "character-publication.v1"
        or snapshot.get("character_version_id") != str(version["id"])
        or not views_compatible
        or not isinstance(snapshot.get("assets_by_view"), dict)
    ):
        raise invalid_publication()
    return snapshot, publication_hash


def invalid_publication() -> HTTPException:
    return reference_error(
        409,
        "CHARACTER_PUBLICATION_INVALID",
        "当前人物版本的发布快照无效，请管理员重新检查。",
    )


def load_published_reference_assets(
    conn: BusinessConnection,
    *,
    version_id: str,
    publication_snapshot: dict[str, object],
) -> dict[RequiredCharacterViewType, PublishedReferenceAsset]:
    rows = conn.execute(
        """
        SELECT
            character_asset.id AS character_asset_id,
            character_asset.asset_id,
            character_asset.view_type,
            character_asset.review_status,
            asset.storage_uri,
            asset.sha256,
            asset.size_bytes,
            asset.content_type
        FROM character_assets AS character_asset
        JOIN assets AS asset ON asset.id = character_asset.asset_id
        WHERE character_asset.character_version_id = %s
          AND character_asset.is_published_selection = 1
        """,
        (version_id,),
    ).fetchall()
    assets_by_view: dict[RequiredCharacterViewType, PublishedReferenceAsset] = {}
    snapshot_assets = publication_snapshot.get("assets_by_view")
    if not isinstance(snapshot_assets, dict):
        raise invalid_publication()
    for row in rows:
        view = str(row["view_type"])
        if str(row["review_status"]) != "APPROVED":
            raise invalid_publication()
        if view not in REQUIRED_CHARACTER_VIEW_TYPES:
            # 存量七资产人物的镜像右侧视图行：保留在库，不再参与引用选择。
            continue
        typed_view = view
        try:
            size_bytes = int(row["size_bytes"])
        except (TypeError, ValueError) as exc:
            raise invalid_publication() from exc
        if (
            row["asset_id"] is None
            or row["storage_uri"] is None
            or row["sha256"] is None
            or str(row["content_type"]) != "image/png"
            or len(str(row["sha256"])) != 64
            or size_bytes <= 0
        ):
            raise invalid_publication()
        asset = PublishedReferenceAsset(
            character_asset_id=str(row["character_asset_id"]),
            asset_id=str(row["asset_id"]),
            view_type=typed_view,
            storage_uri=str(row["storage_uri"]),
            sha256=str(row["sha256"]),
            size_bytes=size_bytes,
            content_type=str(row["content_type"]),
        )
        snapshot_asset = snapshot_assets.get(view)
        if not publication_asset_matches(asset, snapshot_asset):
            raise invalid_publication()
        assets_by_view[typed_view] = asset
    if not set(REQUIRED_CHARACTER_VIEW_TYPES).issubset(set(assets_by_view)):
        raise invalid_publication()
    return assets_by_view


def publication_asset_matches(
    asset: PublishedReferenceAsset,
    snapshot_asset: object,
) -> bool:
    return (
        isinstance(snapshot_asset, dict)
        and snapshot_asset.get("approved_asset_id") == asset.asset_id
        and snapshot_asset.get("character_asset_id") == asset.character_asset_id
        and snapshot_asset.get("content_type") == asset.content_type
        and snapshot_asset.get("sha256") == asset.sha256
        and snapshot_asset.get("size_bytes") == asset.size_bytes
        and snapshot_asset.get("storage_uri") == asset.storage_uri
    )


def normalize_selected_asset_ids(values: list[str]) -> list[str]:
    selected = unique_ids([value.strip() for value in values if value.strip()])
    if not 1 <= len(selected) <= 4:
        raise reference_error(
            422,
            "CHARACTER_REFERENCE_COUNT_INVALID",
            "请选择 1 到 4 张人物参考图。",
        )
    return selected


def unique_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def latest_character_reference_selection_row(
    conn: BusinessConnection,
    *,
    project_id: str,
) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM character_reference_selections
        WHERE project_id = %s
        ORDER BY selected_at DESC, id DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else cast(sqlite3.Row, row)


def read_character_reference_selection_row(
    conn: BusinessConnection,
    *,
    selection_id: str,
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM character_reference_selections WHERE id = %s",
        (selection_id,),
    ).fetchone()
    if row is None:
        raise reference_error(
            404,
            "CHARACTER_REFERENCE_SELECTION_NOT_FOUND",
            "人物参考图选择不存在。",
        )
    return cast(sqlite3.Row, row)


def selection_row_matches(row: sqlite3.Row, inputs: ReferenceSelectionInputs) -> bool:
    return (
        str(row["source_frame_version_id"]) == inputs.source_frame_version_id
        and str(row["character_version_id"]) == inputs.character_version_id
        and decode_string_list(row["recommended_asset_ids_json"]) == inputs.recommended_asset_ids
        and decode_string_list(row["selected_asset_ids_json"]) == inputs.selected_asset_ids
        and decode_object(row["recommendation_reason_json"]) == inputs.recommendation_reason
        and decode_object(row["character_version_snapshot_json"])
        == inputs.character_version_snapshot
    )


def character_reference_selection_from_row(row: sqlite3.Row) -> CharacterReferenceSelection:
    return CharacterReferenceSelection(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        source_frame_version_id=str(row["source_frame_version_id"]),
        character_version_id=str(row["character_version_id"]),
        recommended_asset_ids_json=decode_string_list(row["recommended_asset_ids_json"]),
        selected_asset_ids_json=decode_string_list(row["selected_asset_ids_json"]),
        recommendation_reason_json=decode_object(row["recommendation_reason_json"]),
        character_version_snapshot_json=decode_object(row["character_version_snapshot_json"]),
        selected_by=None if row["selected_by"] is None else str(row["selected_by"]),
        selected_at=parse_datetime(str(row["selected_at"])),
    )


def decode_string_list(value: object) -> list[str]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return []
    return cast(list[str], decoded)


def insert_reference_audit(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    selection_id: str,
    project_id: str,
    inputs: ReferenceSelectionInputs,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (
            id, actor_user_id, action, entity_type, entity_id, metadata_json
        ) VALUES (%s, %s, 'character_reference.select', 'character_reference_selection', %s, %s)
        """,
        (
            str(uuid4()),
            actor.id,
            selection_id,
            encode_json(
                {
                    "project_id": project_id,
                    "source_frame_version_id": inputs.source_frame_version_id,
                    "character_version_id": inputs.character_version_id,
                    "selected_asset_ids": inputs.selected_asset_ids,
                }
            ),
        ),
    )


def reference_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def stale_reference_selection() -> HTTPException:
    return reference_error(
        409,
        "CHARACTER_REFERENCE_SELECTION_STALE",
        "人物参考图选择已过期，请按当前源画面和人物版本重新选择。",
    )
