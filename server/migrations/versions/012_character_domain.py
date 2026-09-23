from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection, RowMapping

revision = "012_character_domain"
down_revision = "011_add_archive_retry_count"
branch_labels = None
depends_on = None

LEGACY_IDENTITY_PREFIX = "legacy-identity:"
LEGACY_PERSONA_PREFIX = "legacy-persona:"
LEGACY_VERSION_PREFIX = "legacy-version:"
LEGACY_ASSET_PREFIX = "legacy-character-asset:"


def _created_at() -> sa.Column[str]:
    return sa.Column(
        "created_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def _updated_at() -> sa.Column[str]:
    return sa.Column(
        "updated_at",
        sa.Text(),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


def upgrade() -> None:
    _create_domain_tables()
    _add_project_character_version_reference()
    _import_legacy_characters(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "idx_project_main_characters_character_version",
        table_name="project_main_characters",
    )
    with op.batch_alter_table("project_main_characters") as batch_op:
        batch_op.drop_constraint(
            "fk_project_main_characters_character_version_id",
            type_="foreignkey",
        )
        batch_op.drop_column("character_version_id")

    op.drop_table("character_reference_selections")
    op.drop_table("character_asset_reviews")
    op.drop_table("character_assets")
    op.drop_table("character_generation_tasks")
    op.drop_table("character_versions")
    op.drop_table("character_personas")
    op.drop_table("person_identities")


def _create_domain_tables() -> None:
    op.create_table(
        "person_identities",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner_user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("authorization_status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column(
            "authorization_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
        ),
        sa.Column("authorization_scope", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("authorization_expires_at", sa.Text()),
        sa.Column("source_asset_id", sa.Text(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("source_quality_status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "authorization_status IN ('PENDING', 'AUTHORIZED', 'EXPIRED', 'REVOKED')",
            name="ck_person_identities_authorization_status",
        ),
        sa.CheckConstraint(
            "source_quality_status IN ('PENDING', 'PASSED', 'FAILED', 'IMPORTED')",
            name="ck_person_identities_source_quality_status",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'ACTIVE', 'EXPIRED', 'REVOKED', 'ARCHIVED')",
            name="ck_person_identities_status",
        ),
    )
    op.create_index(
        "idx_person_identities_availability",
        "person_identities",
        ["status", "authorization_status", "authorization_expires_at"],
    )
    op.create_index("idx_person_identities_owner", "person_identities", ["owner_user_id"])

    op.create_table(
        "character_personas",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("occupation", sa.Text()),
        sa.Column("scene_description", sa.Text()),
        sa.Column("appearance_constraints_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("costume_description", sa.Text()),
        sa.Column("default_background", sa.Text()),
        sa.Column("positive_prompt", sa.Text()),
        sa.Column("negative_prompt", sa.Text()),
        sa.Column("usage_scope_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created_at(),
        _updated_at(),
    )
    op.create_index("idx_character_personas_identity", "character_personas", ["identity_id"])

    op.create_table(
        "character_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "persona_id",
            sa.Text(),
            sa.ForeignKey("character_personas.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="DRAFT"),
        sa.Column("source_asset_id", sa.Text(), sa.ForeignKey("assets.id", ondelete="SET NULL")),
        sa.Column("source_sha256", sa.Text()),
        sa.Column("persona_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("provider", sa.Text()),
        sa.Column("model", sa.Text()),
        sa.Column("generation_params_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("template_version", sa.Text()),
        sa.Column("template_hash", sa.Text()),
        sa.Column("required_view_types_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("published_by", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("published_at", sa.Text()),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created_at(),
        sa.CheckConstraint("version_number > 0", name="ck_character_versions_number"),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'GENERATING', 'REVIEWING', 'PUBLISHED', 'FAILED', 'ARCHIVED')",
            name="ck_character_versions_status",
        ),
    )
    op.create_index(
        "uq_character_versions_persona_version",
        "character_versions",
        ["persona_id", "version_number"],
        unique=True,
    )
    op.create_index(
        "idx_character_versions_persona_status",
        "character_versions",
        ["persona_id", "status"],
    )

    op.create_table(
        "character_generation_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "character_version_id",
            sa.Text(),
            sa.ForeignKey("character_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("view_type", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("request_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("provider_task_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("cost_amount", sa.Float()),
        sa.Column("started_at", sa.Text()),
        sa.Column("completed_at", sa.Text()),
        sa.CheckConstraint(
            "view_type IN ('FRONT_FACE', 'FRONT_HALF', 'FRONT_FULL', 'LEFT_45', "
            "'RIGHT_45', 'LEFT_SIDE', 'RIGHT_SIDE')",
            name="ck_character_generation_tasks_view_type",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')",
            name="ck_character_generation_tasks_status",
        ),
        sa.CheckConstraint(
            "cost_amount IS NULL OR cost_amount >= 0",
            name="ck_character_generation_tasks_cost",
        ),
    )
    op.create_index(
        "idx_character_generation_tasks_version_status",
        "character_generation_tasks",
        ["character_version_id", "status"],
    )
    op.create_index(
        "idx_character_generation_tasks_provider_task",
        "character_generation_tasks",
        ["provider", "provider_task_id"],
    )

    op.create_table(
        "character_assets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "character_version_id",
            sa.Text(),
            sa.ForeignKey("character_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="SET NULL"),
        ),
        sa.Column("view_type", sa.Text(), nullable=False),
        sa.Column("candidate_number", sa.Integer(), nullable=False),
        sa.Column(
            "generation_task_id",
            sa.Text(),
            sa.ForeignKey("character_generation_tasks.id", ondelete="SET NULL"),
        ),
        sa.Column("auto_quality_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("review_status", sa.Text(), nullable=False, server_default="NOT_REVIEWED"),
        sa.Column("is_published_selection", sa.Integer(), nullable=False, server_default="0"),
        _created_at(),
        sa.CheckConstraint("candidate_number > 0", name="ck_character_assets_candidate_number"),
        sa.CheckConstraint(
            "view_type IN ('FRONT_FACE', 'FRONT_HALF', 'FRONT_FULL', 'LEFT_45', "
            "'RIGHT_45', 'LEFT_SIDE', 'RIGHT_SIDE', 'IMPORTED_REFERENCE')",
            name="ck_character_assets_view_type",
        ),
        sa.CheckConstraint(
            "review_status IN ('NOT_REVIEWED', 'APPROVED', 'REJECTED')",
            name="ck_character_assets_review_status",
        ),
        sa.CheckConstraint(
            "is_published_selection IN (0, 1)",
            name="ck_character_assets_published_selection",
        ),
    )
    op.create_index(
        "idx_character_assets_version_view",
        "character_assets",
        ["character_version_id", "view_type", "candidate_number"],
    )
    op.create_index(
        "idx_character_assets_review",
        "character_assets",
        ["character_version_id", "review_status"],
    )
    op.create_index(
        "uq_character_assets_published_view",
        "character_assets",
        ["character_version_id", "view_type"],
        unique=True,
        sqlite_where=sa.text("is_published_selection = 1"),
        postgresql_where=sa.text("is_published_selection = 1"),
    )

    op.create_table(
        "character_asset_reviews",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "character_asset_id",
            sa.Text(),
            sa.ForeignKey("character_assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "reviewer_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("issue_codes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("comment", sa.Text()),
        _created_at(),
        sa.CheckConstraint(
            "decision IN ('APPROVED', 'REJECTED')",
            name="ck_character_asset_reviews_decision",
        ),
    )
    op.create_index(
        "idx_character_asset_reviews_asset_created",
        "character_asset_reviews",
        ["character_asset_id", "created_at"],
    )

    op.create_table(
        "character_reference_selections",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_frame_version_id",
            sa.Text(),
            sa.ForeignKey("versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "character_version_id",
            sa.Text(),
            sa.ForeignKey("character_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("recommended_asset_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("selected_asset_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("recommendation_reason_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("selected_by", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column(
            "selected_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_character_reference_selections_project",
        "character_reference_selections",
        ["project_id", "selected_at"],
    )
    op.create_index(
        "idx_character_reference_selections_source_version",
        "character_reference_selections",
        ["source_frame_version_id"],
    )


def _add_project_character_version_reference() -> None:
    with op.batch_alter_table("project_main_characters") as batch_op:
        batch_op.add_column(sa.Column("character_version_id", sa.Text()))
        batch_op.create_foreign_key(
            "fk_project_main_characters_character_version_id",
            "character_versions",
            ["character_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_project_main_characters_character_version",
        "project_main_characters",
        ["character_version_id"],
    )


def _import_legacy_characters(connection: Connection) -> None:
    rows = connection.execute(
        sa.text("SELECT * FROM characters ORDER BY created_at, id")
    ).mappings()
    known_assets = {
        str(row[0]) for row in connection.execute(sa.text("SELECT id FROM assets")).fetchall()
    }
    for row in rows:
        _import_legacy_character(connection, row, known_assets)

    connection.execute(
        sa.text(
            """
            UPDATE project_main_characters
            SET character_version_id = :prefix || character_id
            WHERE character_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM character_versions
                  WHERE character_versions.id = :prefix || project_main_characters.character_id
              )
            """
        ),
        {"prefix": LEGACY_VERSION_PREFIX},
    )


def _import_legacy_character(
    connection: Connection,
    row: RowMapping,
    known_assets: set[str],
) -> None:
    character_id = str(row["id"])
    identity_id = f"{LEGACY_IDENTITY_PREFIX}{character_id}"
    persona_id = f"{LEGACY_PERSONA_PREFIX}{character_id}"
    version_id = f"{LEGACY_VERSION_PREFIX}{character_id}"
    references, _ = _decode_string_list(row["reference_asset_ids_json"])
    scopes, scope_is_valid = _decode_string_list(row["authorization_project_ids_json"])
    existing_references = [asset_id for asset_id in references if asset_id in known_assets]
    source_asset_id = existing_references[0] if existing_references else None
    source_sha256 = _asset_sha256(connection, source_asset_id)
    is_active = bool(row["is_active"])
    status, authorization_status = _legacy_identity_status(
        is_active=is_active,
        expires_at=None
        if row["authorization_expires_at"] is None
        else str(row["authorization_expires_at"]),
        scope_is_valid=scope_is_valid,
    )
    snapshot = {
        "id": character_id,
        "name": str(row["name"]),
        "reference_asset_ids": references,
        "authorization_project_ids": scopes,
        "authorization_expires_at": row["authorization_expires_at"],
        "is_active": is_active,
    }
    snapshot_json = _encode_json(snapshot)
    scope_json = _encode_json(scopes)
    actor_id = row["created_by_user_id"]
    created_at = str(row["created_at"])
    updated_at = str(row["updated_at"])

    connection.execute(
        sa.text(
            """
            INSERT INTO person_identities (
                id, owner_user_id, display_name, authorization_status,
                authorization_scope, authorization_expires_at, source_asset_id,
                source_quality_status, status, created_by, created_at, updated_at
            )
            VALUES (
                :id, :owner_user_id, :display_name, :authorization_status,
                :authorization_scope, :authorization_expires_at, :source_asset_id,
                'IMPORTED', :status, :created_by, :created_at, :updated_at
            )
            """
        ),
        {
            "id": identity_id,
            "owner_user_id": actor_id,
            "display_name": str(row["name"]),
            "authorization_status": authorization_status,
            "authorization_scope": scope_json,
            "authorization_expires_at": row["authorization_expires_at"],
            "source_asset_id": source_asset_id,
            "status": status,
            "created_by": actor_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO character_personas (
                id, identity_id, name, appearance_constraints_json,
                usage_scope_json, created_by, created_at, updated_at
            )
            VALUES (
                :id, :identity_id, :name, '{}', :usage_scope,
                :created_by, :created_at, :updated_at
            )
            """
        ),
        {
            "id": persona_id,
            "identity_id": identity_id,
            "name": str(row["name"]),
            "usage_scope": scope_json,
            "created_by": actor_id,
            "created_at": created_at,
            "updated_at": updated_at,
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO character_versions (
                id, persona_id, version_number, status, source_asset_id,
                source_sha256, persona_snapshot_json, provider, model,
                generation_params_json, required_view_types_json,
                published_by, published_at, created_by, created_at
            )
            VALUES (
                :id, :persona_id, 1, 'PUBLISHED', :source_asset_id,
                :source_sha256, :snapshot, 'legacy-import', 'legacy-character-v1',
                '{}', '[]', :published_by, :published_at, :created_by, :created_at
            )
            """
        ),
        {
            "id": version_id,
            "persona_id": persona_id,
            "source_asset_id": source_asset_id,
            "source_sha256": source_sha256,
            "snapshot": snapshot_json,
            "published_by": actor_id,
            "published_at": created_at,
            "created_by": actor_id,
            "created_at": created_at,
        },
    )
    for candidate_number, asset_id in enumerate(existing_references, start=1):
        connection.execute(
            sa.text(
                """
                INSERT INTO character_assets (
                    id, character_version_id, asset_id, view_type,
                    candidate_number, auto_quality_json, review_status,
                    is_published_selection, created_at
                )
                VALUES (
                    :id, :character_version_id, :asset_id, 'IMPORTED_REFERENCE',
                    :candidate_number, '{}', 'APPROVED', :is_published_selection,
                    :created_at
                )
                """
            ),
            {
                "id": f"{LEGACY_ASSET_PREFIX}{character_id}:{candidate_number}",
                "character_version_id": version_id,
                "asset_id": asset_id,
                "candidate_number": candidate_number,
                "is_published_selection": 1 if candidate_number == 1 else 0,
                "created_at": created_at,
            },
        )


def _decode_string_list(raw: Any) -> tuple[list[str], bool]:
    try:
        decoded = json.loads(str(raw))
    except json.JSONDecodeError:
        return [], False
    if not isinstance(decoded, list):
        return [], False
    return [str(value) for value in decoded], True


def _encode_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _asset_sha256(connection: Connection, asset_id: str | None) -> str | None:
    if asset_id is None:
        return None
    row = connection.execute(
        sa.text("SELECT sha256 FROM assets WHERE id = :asset_id"),
        {"asset_id": asset_id},
    ).fetchone()
    return None if row is None else str(row[0])


def _legacy_identity_status(
    *,
    is_active: bool,
    expires_at: str | None,
    scope_is_valid: bool,
) -> tuple[str, str]:
    if not is_active or not scope_is_valid:
        return "REVOKED", "REVOKED"
    if expires_at is not None:
        try:
            expiration = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiration.tzinfo is None:
                expiration = expiration.replace(tzinfo=UTC)
            if expiration <= datetime.now(UTC):
                return "EXPIRED", "EXPIRED"
        except ValueError:
            return "EXPIRED", "EXPIRED"
    return "ACTIVE", "AUTHORIZED"
