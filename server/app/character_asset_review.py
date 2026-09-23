from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from fastapi import HTTPException

from app import content_store
from app.auth import CurrentUser
from app.character_contracts import (
    CharacterAssetReview,
    CharacterAssetReviewDecision,
    CharacterVersion,
    RequiredCharacterViewType,
)
from app.character_identity import (
    REQUIRED_CHARACTER_VIEW_TYPES,
    approved_character_asset_key,
    character_error,
    character_not_found,
    decode_string_list,
    encode_json,
    get_character_version,
    parse_datetime,
    read_identity_row,
    read_persona_row,
    read_version_row,
    require_character_admin,
    require_identity_active,
)
from app.db_portable import BusinessConnection
from app.permissions import require_role
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    require_storage_match,
    storage_object_ref_from_uri,
)

logger = logging.getLogger(__name__)

CHARACTER_PUBLICATION_SCHEMA_VERSION = "character-publication.v1"
MUTABLE_REVIEW_STATUSES = {"DRAFT", "GENERATING", "REVIEWING", "FAILED"}
ISSUE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class CharacterPublicationAlreadyCompleted(RuntimeError):
    pass


@dataclass(frozen=True)
class SelectedCharacterAsset:
    character_asset_id: str
    view_type: RequiredCharacterViewType
    generated_asset_id: str
    storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str
    review_id: str


@dataclass(frozen=True)
class PreparedPublicationAsset:
    selected: SelectedCharacterAsset
    approved_asset_id: str
    approved_storage_uri: str
    sha256: str
    size_bytes: int
    content_type: str


def review_character_asset(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    character_asset_id: str,
    decision: CharacterAssetReviewDecision,
    issue_codes: list[str],
    comment: str | None,
) -> CharacterAssetReview:
    require_character_admin(
        conn,
        actor=actor,
        action="character_asset.review",
        entity_type="character_asset",
        entity_id=character_asset_id,
    )
    normalized_issues = normalize_issue_codes(issue_codes)
    normalized_comment = normalize_review_comment(comment)
    if decision == "REJECTED" and not normalized_issues and normalized_comment is None:
        raise character_error(
            422,
            "CHARACTER_ASSET_REVIEW_REASON_REQUIRED",
            "驳回人物资产时至少填写一个问题码或审核说明。",
        )

    review_id = str(uuid4())
    try:
        conn.execute("BEGIN IMMEDIATE")
        asset = read_character_asset_row(conn, character_asset_id)
        version = read_version_row(conn, str(asset["character_version_id"]))
        require_version_review_mutable(version)
        conn.execute(
            """
            INSERT INTO character_asset_reviews (
                id, character_asset_id, reviewer_user_id,
                decision, issue_codes_json, comment
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                review_id,
                character_asset_id,
                actor.id,
                decision,
                encode_json(normalized_issues),
                normalized_comment,
            ),
        )
        conn.execute(
            "UPDATE character_assets SET review_status = %s WHERE id = %s",
            (decision, character_asset_id),
        )
        insert_audit(
            conn,
            actor=actor,
            action="character_asset.review",
            entity_type="character_asset",
            entity_id=character_asset_id,
            metadata={
                "decision": decision,
                "issue_codes": normalized_issues,
                "review_id": review_id,
            },
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_character_asset_review(conn, review_id)


def _review_order_token(conn: BusinessConnection) -> str:
    """Append-only 插入序伪列，按通道选择。

    审核历史是只增不改的表，「最新一次裁决」依赖插入序打平同一
    ``created_at`` 的行：SQLite 用 ``rowid``；PostgreSQL 没有 ``rowid``，
    ``ctid`` 是官方对应的物理插入序伪列。CW-058 之前 PG 通道上任何审核
    历史查询/发布路径都会因 ``column "rowid" does not exist`` 直接失败。
    """
    return "ctid" if conn.is_postgres else "rowid"


def list_character_asset_reviews(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    character_asset_id: str,
) -> list[CharacterAssetReview]:
    require_role(
        conn,
        actor=actor,
        allowed_roles={"admin", "auditor"},
        action="character_asset.review.list",
        entity_type="character_asset",
        entity_id=character_asset_id,
    )
    read_character_asset_row(conn, character_asset_id)
    rows = conn.execute(
        f"""
        SELECT * FROM character_asset_reviews
        WHERE character_asset_id = %s
        ORDER BY created_at, {_review_order_token(conn)}
        """,
        (character_asset_id,),
    ).fetchall()
    return [character_asset_review_from_row(row) for row in rows]


def publish_character_version(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
    selected_asset_ids: dict[RequiredCharacterViewType, str],
    storage: StorageAdapter,
) -> CharacterVersion:
    require_character_admin(
        conn,
        actor=actor,
        action="character_version.publish",
        entity_type="character_version",
        entity_id=version_id,
    )
    version = read_version_row(conn, version_id)
    if str(version["status"]) == "PUBLISHED":
        require_published_selection_matches(
            conn,
            version_id=version_id,
            selected_asset_ids=selected_asset_ids,
        )
        return get_character_version(conn, actor=actor, version_id=version_id)
    required_views = require_standard_publication_views(version)
    require_complete_publication_selection(required_views, selected_asset_ids)
    require_no_active_character_tasks(conn, version_id)
    require_version_publishable(version)
    persona = read_persona_row(conn, str(version["persona_id"]))
    identity = read_identity_row(conn, str(persona["identity_id"]))
    require_identity_active(identity)
    owner_user_id = effective_owner_user_id(identity, fallback=actor.id)
    selected = load_selected_character_assets(
        conn,
        version_id=version_id,
        required_views=required_views,
        selected_asset_ids=selected_asset_ids,
    )

    prepared, attempted_keys = prepare_approved_publication_objects(
        selected,
        storage=storage,
        owner_user_id=owner_user_id,
        persona_id=str(version["persona_id"]),
        version_id=version_id,
    )

    try:
        published_at = datetime.now(UTC).isoformat()
        snapshot = publication_snapshot(prepared, version=version, published_at=published_at)
        snapshot_json = encode_json(snapshot)
        publication_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
        conn.execute("BEGIN IMMEDIATE")
        latest_version = read_version_row(conn, version_id)
        if str(latest_version["status"]) == "PUBLISHED":
            require_published_selection_matches(
                conn,
                version_id=version_id,
                selected_asset_ids=selected_asset_ids,
            )
            raise CharacterPublicationAlreadyCompleted
        require_no_active_character_tasks(conn, version_id)
        require_version_publishable(latest_version)
        latest_persona = read_persona_row(conn, str(latest_version["persona_id"]))
        latest_identity = read_identity_row(conn, str(latest_persona["identity_id"]))
        require_identity_active(latest_identity)
        if effective_owner_user_id(latest_identity, fallback=actor.id) != owner_user_id:
            raise character_error(
                409,
                "CHARACTER_PUBLICATION_SOURCE_CHANGED",
                "发布期间人物资产归属已变更，请重新检查。",
            )
        latest_selected = load_selected_character_assets(
            conn,
            version_id=version_id,
            required_views=required_views,
            selected_asset_ids=selected_asset_ids,
        )
        require_publication_sources_unchanged(prepared, latest_selected)

        conn.execute(
            """
            UPDATE character_assets
            SET is_published_selection = 0
            WHERE character_version_id = %s
            """,
            (version_id,),
        )
        for item in prepared:
            conn.execute(
                """
                INSERT INTO assets (
                    id, project_id, kind, storage_uri, sha256, size_bytes,
                    content_type, created_by_user_id, metadata_json
                ) VALUES (%s, NULL, 'character_approved_image', %s, %s, %s, %s, %s, %s)
                """,
                (
                    item.approved_asset_id,
                    item.approved_storage_uri,
                    item.sha256,
                    item.size_bytes,
                    item.content_type,
                    actor.id,
                    encode_json(
                        {
                            "character_asset_id": item.selected.character_asset_id,
                            "character_version_id": version_id,
                            "generated_asset_id": item.selected.generated_asset_id,
                            "publication_hash": publication_hash,
                            "view_type": item.selected.view_type,
                        }
                    ),
                ),
            )
            updated = conn.execute(
                """
                UPDATE character_assets
                SET asset_id = %s, is_published_selection = 1
                WHERE id = %s AND character_version_id = %s
                  AND asset_id = %s AND review_status = 'APPROVED'
                """,
                (
                    item.approved_asset_id,
                    item.selected.character_asset_id,
                    version_id,
                    item.selected.generated_asset_id,
                ),
            )
            if updated.rowcount != 1:
                raise character_error(
                    409,
                    "CHARACTER_PUBLICATION_SOURCE_CHANGED",
                    "发布期间人物候选资产已变更，请重新检查。",
                )

        updated_version = conn.execute(
            """
            UPDATE character_versions
            SET status = 'PUBLISHED', published_by = %s, published_at = %s,
                publication_snapshot_json = %s, publication_hash = %s
            WHERE id = %s AND status = 'REVIEWING'
            """,
            (actor.id, published_at, snapshot_json, publication_hash, version_id),
        )
        if updated_version.rowcount != 1:
            raise character_error(
                409,
                "CHARACTER_VERSION_NOT_REVIEWING",
                "只有待审核的角色版本可以发布。",
            )
        insert_audit(
            conn,
            actor=actor,
            action="character_version.publish",
            entity_type="character_version",
            entity_id=version_id,
            metadata={
                "publication_hash": publication_hash,
                "selected_character_asset_ids": [
                    item.selected.character_asset_id for item in prepared
                ],
            },
        )
        conn.commit()
    except CharacterPublicationAlreadyCompleted:
        conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        return get_character_version(conn, actor=actor, version_id=version_id)
    except Exception:
        conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise
    return get_character_version(conn, actor=actor, version_id=version_id)


def prepare_approved_publication_objects(
    selected: list[SelectedCharacterAsset],
    *,
    storage: StorageAdapter,
    owner_user_id: str,
    persona_id: str,
    version_id: str,
) -> tuple[list[PreparedPublicationAsset], list[str]]:
    prepared: list[PreparedPublicationAsset] = []
    attempted_keys: list[str] = []
    try:
        for item in selected:
            reference = storage_object_ref_from_uri(item.storage_uri)
            require_storage_match(storage, reference)
            content = storage.get_object(reference.key)
            if hashlib.sha256(content).hexdigest() != item.sha256:
                raise character_error(
                    409,
                    "CHARACTER_PUBLICATION_SOURCE_CHANGED",
                    "人物候选资产内容与已保存哈希不一致。",
                )
            approved_asset_id = str(uuid4())
            approved_key = approved_character_asset_key(
                owner_user_id=owner_user_id,
                persona_id=persona_id,
                version_id=version_id,
                view_type=item.view_type,
                asset_id=approved_asset_id,
            )
            attempted_keys.append(approved_key)
            stored = storage.put_object(approved_key, content, content_type=item.content_type)
            if stored.sha256 != item.sha256:
                raise StorageBackendUnavailable("approved character asset hash mismatch")
            prepared.append(
                PreparedPublicationAsset(
                    selected=item,
                    approved_asset_id=approved_asset_id,
                    approved_storage_uri=stored.uri,
                    sha256=stored.sha256,
                    size_bytes=stored.size,
                    content_type=stored.content_type,
                )
            )
    except HTTPException:
        cleanup_publication_objects(storage, attempted_keys)
        raise
    except (KeyError, OSError, StorageBackendUnavailable, ValueError) as exc:
        cleanup_publication_objects(storage, attempted_keys)
        logger.warning("Character publication storage failed: %s", type(exc).__name__)
        raise character_error(
            503,
            "CHARACTER_PUBLICATION_STORAGE_UNAVAILABLE",
            "已批准人物资产写入存储失败，本次发布未生效。",
        ) from exc
    return prepared, attempted_keys


def cleanup_publication_objects(storage: StorageAdapter, object_keys: list[str]) -> None:
    for object_key in object_keys:
        try:
            content_store.delete_object_outside_content_namespace(storage, object_key)
        except Exception:
            logger.exception("Failed to clean an orphaned approved character object")


def load_selected_character_assets(
    conn: BusinessConnection,
    *,
    version_id: str,
    required_views: list[RequiredCharacterViewType],
    selected_asset_ids: dict[RequiredCharacterViewType, str],
) -> list[SelectedCharacterAsset]:
    selected: list[SelectedCharacterAsset] = []
    for view_type in required_views:
        character_asset_id = selected_asset_ids[view_type]
        latest_order = _review_order_token(conn)
        row = conn.execute(
            f"""
            SELECT character_asset.*, asset.storage_uri, asset.sha256,
                   asset.size_bytes, asset.content_type,
                   (
                       SELECT review.id
                       FROM character_asset_reviews AS review
                       WHERE review.character_asset_id = character_asset.id
                       ORDER BY review.created_at DESC, review.{latest_order} DESC
                       LIMIT 1
                   ) AS review_id,
                   (
                       SELECT review.decision
                       FROM character_asset_reviews AS review
                       WHERE review.character_asset_id = character_asset.id
                       ORDER BY review.created_at DESC, review.{latest_order} DESC
                       LIMIT 1
                   ) AS review_decision
            FROM character_assets AS character_asset
            LEFT JOIN assets AS asset ON asset.id = character_asset.asset_id
            WHERE character_asset.id = %s
            """,
            (character_asset_id,),
        ).fetchone()
        if row is None or str(row["character_version_id"]) != version_id:
            raise character_error(
                409,
                "CHARACTER_PUBLISH_ASSET_NOT_FOUND",
                "选中的人物候选资产不属于当前版本。",
                view_type=view_type,
            )
        if str(row["view_type"]) != view_type:
            raise character_error(
                409,
                "CHARACTER_PUBLISH_ASSET_VIEW_MISMATCH",
                "选中的人物候选资产与目标视角不匹配。",
                view_type=view_type,
            )
        if (
            str(row["review_status"]) != "APPROVED"
            or row["review_id"] is None
            or str(row["review_decision"]) != "APPROVED"
        ):
            raise character_error(
                409,
                "CHARACTER_PUBLISH_SELECTION_NOT_APPROVED",
                "每个必需视角都必须选择一张已人工批准的资产。",
                view_type=view_type,
            )
        if row["asset_id"] is None or row["storage_uri"] is None or row["sha256"] is None:
            raise character_error(
                409,
                "CHARACTER_PUBLISH_ASSET_MISSING",
                "选中的人物候选资产文件不完整。",
                view_type=view_type,
            )
        if (
            row["content_type"] != "image/png"
            or row["size_bytes"] is None
            or int(row["size_bytes"]) <= 0
            or len(str(row["sha256"])) != 64
        ):
            raise character_error(
                409,
                "CHARACTER_PUBLISH_ASSET_INVALID",
                "选中的人物候选资产必须是完整的 PNG 图片。",
                view_type=view_type,
            )
        selected.append(
            SelectedCharacterAsset(
                character_asset_id=character_asset_id,
                view_type=view_type,
                generated_asset_id=str(row["asset_id"]),
                storage_uri=str(row["storage_uri"]),
                sha256=str(row["sha256"]),
                size_bytes=int(row["size_bytes"]),
                content_type=str(row["content_type"]),
                review_id=str(row["review_id"]),
            )
        )
    return selected


def publication_snapshot(
    prepared: list[PreparedPublicationAsset],
    *,
    version: sqlite3.Row,
    published_at: str,
) -> dict[str, object]:
    return {
        "assets_by_view": {
            item.selected.view_type: {
                "approved_asset_id": item.approved_asset_id,
                "character_asset_id": item.selected.character_asset_id,
                "content_type": item.content_type,
                "generated_asset_id": item.selected.generated_asset_id,
                "review_id": item.selected.review_id,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "storage_uri": item.approved_storage_uri,
            }
            for item in prepared
        },
        "character_version_id": str(version["id"]),
        "persona_snapshot_hash": hashlib.sha256(
            str(version["persona_snapshot_json"]).encode()
        ).hexdigest(),
        "published_at": published_at,
        "required_view_types": [item.selected.view_type for item in prepared],
        "schema_version": CHARACTER_PUBLICATION_SCHEMA_VERSION,
        "template_hash": version["template_hash"],
        "template_version": version["template_version"],
    }


def require_publication_sources_unchanged(
    prepared: list[PreparedPublicationAsset],
    latest: list[SelectedCharacterAsset],
) -> None:
    expected = {
        item.selected.view_type: (
            item.selected.character_asset_id,
            item.selected.generated_asset_id,
            item.selected.sha256,
            item.selected.review_id,
        )
        for item in prepared
    }
    actual = {
        item.view_type: (
            item.character_asset_id,
            item.generated_asset_id,
            item.sha256,
            item.review_id,
        )
        for item in latest
    }
    if actual != expected:
        raise character_error(
            409,
            "CHARACTER_PUBLICATION_SOURCE_CHANGED",
            "发布期间人物候选或审核结果已变更，请重新检查。",
        )


def require_standard_publication_views(
    version: sqlite3.Row,
) -> list[RequiredCharacterViewType]:
    required = decode_string_list(version["required_view_types_json"])
    if not set(REQUIRED_CHARACTER_VIEW_TYPES).issubset(set(required)):
        raise character_error(
            409,
            "CHARACTER_VERSION_NO_STANDARD_VIEWS",
            "历史导入版本不能套用标准五视角发布流程。",
        )
    return list(REQUIRED_CHARACTER_VIEW_TYPES)


def require_complete_publication_selection(
    required_views: list[RequiredCharacterViewType],
    selected_asset_ids: dict[RequiredCharacterViewType, str],
) -> None:
    if set(selected_asset_ids) != set(required_views):
        raise character_error(
            422,
            "CHARACTER_PUBLISH_SELECTION_INCOMPLETE",
            "发布前必须为五个必需视角各选择一张已批准资产。",
            required_view_types=required_views,
        )


def require_published_selection_matches(
    conn: BusinessConnection,
    *,
    version_id: str,
    selected_asset_ids: dict[RequiredCharacterViewType, str],
) -> None:
    rows = conn.execute(
        """
        SELECT view_type, id
        FROM character_assets
        WHERE character_version_id = %s AND is_published_selection = 1
        """,
        (version_id,),
    ).fetchall()
    published = {str(row["view_type"]): str(row["id"]) for row in rows}
    requested = {str(view_type): asset_id for view_type, asset_id in selected_asset_ids.items()}
    if requested != published:
        raise character_error(
            409,
            "CHARACTER_VERSION_ALREADY_PUBLISHED_DIFFERENT_SELECTION",
            "角色版本已按另一套人物资产发布，不能替换已冻结选择。",
        )


def require_no_active_character_tasks(conn: BusinessConnection, version_id: str) -> None:
    active = conn.execute(
        """
        SELECT 1 FROM character_generation_tasks
        WHERE character_version_id = %s AND status IN ('PENDING', 'RUNNING')
        LIMIT 1
        """,
        (version_id,),
    ).fetchone()
    if active is not None:
        raise character_error(
            409,
            "CHARACTER_PUBLISH_TASKS_ACTIVE",
            "仍有人物候选正在生成或等待重试，完成后再发布。",
        )


def require_version_publishable(version: sqlite3.Row) -> None:
    status = str(version["status"])
    if status == "ARCHIVED":
        raise character_error(409, "CHARACTER_VERSION_IMMUTABLE", "已归档角色版本不能发布。")
    if status != "REVIEWING":
        raise character_error(
            409,
            "CHARACTER_VERSION_NOT_REVIEWING",
            "只有待审核的角色版本可以发布。",
        )


def require_version_review_mutable(version: sqlite3.Row) -> None:
    if str(version["status"]) not in MUTABLE_REVIEW_STATUSES:
        raise character_error(
            409,
            "CHARACTER_VERSION_IMMUTABLE",
            "已发布或已归档的角色版本不能修改审核结果。",
        )


def read_character_asset_row(conn: BusinessConnection, character_asset_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM character_assets WHERE id = %s",
        (character_asset_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("CHARACTER_ASSET_NOT_FOUND", "角色候选资产不存在。")
    return cast(sqlite3.Row, row)


def get_character_asset_review(
    conn: BusinessConnection,
    review_id: str,
) -> CharacterAssetReview:
    row = conn.execute(
        "SELECT * FROM character_asset_reviews WHERE id = %s",
        (review_id,),
    ).fetchone()
    if row is None:
        raise character_not_found("CHARACTER_ASSET_REVIEW_NOT_FOUND", "角色资产审核记录不存在。")
    return character_asset_review_from_row(row)


def character_asset_review_from_row(row: sqlite3.Row) -> CharacterAssetReview:
    return CharacterAssetReview(
        id=str(row["id"]),
        character_asset_id=str(row["character_asset_id"]),
        reviewer_user_id=(
            None if row["reviewer_user_id"] is None else str(row["reviewer_user_id"])
        ),
        decision=cast(CharacterAssetReviewDecision, str(row["decision"])),
        issue_codes_json=decode_string_list(row["issue_codes_json"]),
        comment=None if row["comment"] is None else str(row["comment"]),
        created_at=parse_datetime(str(row["created_at"])),
    )


def normalize_issue_codes(issue_codes: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw in issue_codes:
        code = raw.strip().upper()
        if not ISSUE_CODE_PATTERN.fullmatch(code):
            raise character_error(
                422,
                "CHARACTER_ASSET_REVIEW_ISSUE_CODE_INVALID",
                "审核问题码必须使用大写字母、数字和下划线。",
            )
        if code not in normalized:
            normalized.append(code)
    return normalized


def normalize_review_comment(comment: str | None) -> str | None:
    if comment is None:
        return None
    normalized = comment.strip()
    return normalized or None


def effective_owner_user_id(identity: sqlite3.Row, *, fallback: str) -> str:
    return str(identity["owner_user_id"] or identity["created_by"] or fallback)


def insert_audit(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (
            id, actor_user_id, action, entity_type, entity_id, metadata_json
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            actor.id,
            action,
            entity_type,
            entity_id,
            encode_json(metadata),
        ),
    )
