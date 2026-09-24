from __future__ import annotations

import hashlib

import sqlalchemy as sa
from alembic import op

revision = "013_character_identity_assets"
down_revision = "012_character_domain"
branch_labels = None
depends_on = None

PLACEHOLDER_USER_ID = "migration-character-assets"
PLACEHOLDER_PROJECT_ID = "migration-character-assets-project"
LEGACY_CHARACTER_TEMPLATE_VERSION = "legacy-character-v1"
LEGACY_CHARACTER_TEMPLATE = (
    "Grandfather an imported legacy character snapshot without claiming seven generated views."
)
LEGACY_CHARACTER_TEMPLATE_HASH = hashlib.sha256(LEGACY_CHARACTER_TEMPLATE.encode()).hexdigest()


def upgrade() -> None:
    with op.batch_alter_table("assets") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.Text(),
            existing_nullable=False,
            nullable=True,
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE character_versions
            SET template_version = :template_version, template_hash = :template_hash
            WHERE provider = 'legacy-import'
              AND template_version IS NULL
              AND template_hash IS NULL
            """
        ),
        {
            "template_version": LEGACY_CHARACTER_TEMPLATE_VERSION,
            "template_hash": LEGACY_CHARACTER_TEMPLATE_HASH,
        },
    )
    placeholder_exists = connection.execute(
        sa.text("SELECT 1 FROM projects WHERE id = :project_id"),
        {"project_id": PLACEHOLDER_PROJECT_ID},
    ).fetchone()
    if placeholder_exists is None:
        return

    connection.execute(
        sa.text(
            """
            UPDATE assets
            SET project_id = NULL
            WHERE project_id = :project_id
              AND (
                  id IN (
                      SELECT authorization_asset_id
                      FROM person_identities
                      WHERE authorization_asset_id IS NOT NULL
                      UNION
                      SELECT source_asset_id
                      FROM person_identities
                      WHERE source_asset_id IS NOT NULL
                  )
                  OR id IN (
                      SELECT asset_id
                      FROM character_assets
                      WHERE asset_id IS NOT NULL
                  )
              )
            """
        ),
        {"project_id": PLACEHOLDER_PROJECT_ID},
    )
    remaining = connection.execute(
        sa.text("SELECT 1 FROM assets WHERE project_id = :project_id LIMIT 1"),
        {"project_id": PLACEHOLDER_PROJECT_ID},
    ).fetchone()
    if remaining is None:
        connection.execute(
            sa.text("DELETE FROM projects WHERE id = :project_id"),
            {"project_id": PLACEHOLDER_PROJECT_ID},
        )
        connection.execute(
            sa.text("DELETE FROM users WHERE id = :user_id"),
            {"user_id": PLACEHOLDER_USER_ID},
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE character_versions
            SET template_version = NULL, template_hash = NULL
            WHERE provider = 'legacy-import'
              AND template_version = :template_version
              AND template_hash = :template_hash
            """
        ),
        {
            "template_version": LEGACY_CHARACTER_TEMPLATE_VERSION,
            "template_hash": LEGACY_CHARACTER_TEMPLATE_HASH,
        },
    )
    has_identity_assets = connection.execute(
        sa.text("SELECT 1 FROM assets WHERE project_id IS NULL LIMIT 1")
    ).fetchone()
    if has_identity_assets is not None:
        connection.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO users (
                    id, username, display_name, role, is_active
                )
                VALUES (
                    :user_id, :user_id, 'Character asset migration', 'admin', 0
                )
                """
            ),
            {"user_id": PLACEHOLDER_USER_ID},
        )
        connection.execute(
            sa.text(
                """
                INSERT OR IGNORE INTO projects (id, owner_user_id, name, status)
                VALUES (
                    :project_id, :user_id, 'Character asset migration', 'ARCHIVED'
                )
                """
            ),
            {
                "project_id": PLACEHOLDER_PROJECT_ID,
                "user_id": PLACEHOLDER_USER_ID,
            },
        )
        connection.execute(
            sa.text(
                """
                UPDATE assets
                SET project_id = :project_id
                WHERE project_id IS NULL
                """
            ),
            {"project_id": PLACEHOLDER_PROJECT_ID},
        )

    with op.batch_alter_table("assets") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.Text(),
            existing_nullable=True,
            nullable=False,
        )
