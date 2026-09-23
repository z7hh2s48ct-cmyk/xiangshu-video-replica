"""Backfill identity ownership before making account isolation mandatory.

Project relationships are authoritative because historical imports could be
performed by an administrator on behalf of a project owner.  A remaining
identity falls back to its still-existing creator.  Ambiguous or ownerless
rows abort the migration so production never hides data under a guessed user.

Revision ID: 051_identity_owner_backfill
Revises: 050_activation_license_zero_credit
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "051_identity_owner_backfill"
down_revision = "050_activation_license_zero_credit"
branch_labels = None
depends_on = None


_RELATIONSHIP_CANDIDATES = """
    SELECT identity.id AS identity_id, project.owner_user_id
    FROM person_identities AS identity
    JOIN assets AS asset
      ON asset.id IN (identity.source_asset_id, identity.authorization_asset_id)
    JOIN projects AS project ON project.id = asset.project_id
    WHERE identity.owner_user_id IS NULL

    UNION

    SELECT identity.id AS identity_id, project.owner_user_id
    FROM person_identities AS identity
    JOIN character_personas AS persona ON persona.identity_id = identity.id
    JOIN character_versions AS version ON version.persona_id = persona.id
    JOIN project_main_characters AS selection
      ON selection.character_version_id = version.id
    JOIN projects AS project ON project.id = selection.project_id
    WHERE identity.owner_user_id IS NULL

    UNION

    SELECT identity.id AS identity_id, project.owner_user_id
    FROM person_identities AS identity
    JOIN project_main_characters AS selection
      ON identity.id = 'legacy-identity:' || selection.character_id
    JOIN projects AS project ON project.id = selection.project_id
    WHERE identity.owner_user_id IS NULL

    UNION

    SELECT identity.id AS identity_id, project.owner_user_id
    FROM person_identities AS identity
    JOIN character_personas AS persona ON persona.identity_id = identity.id
    JOIN character_versions AS version ON version.persona_id = persona.id
    JOIN character_reference_selections AS selection
      ON selection.character_version_id = version.id
    JOIN projects AS project ON project.id = selection.project_id
    WHERE identity.owner_user_id IS NULL
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    conflicts = bind.execute(
        sa.text(
            "WITH relationship_candidates AS (" + _RELATIONSHIP_CANDIDATES + ") "
            "SELECT identity_id, count(DISTINCT owner_user_id) AS owner_count "
            "FROM relationship_candidates GROUP BY identity_id "
            "HAVING count(DISTINCT owner_user_id) > 1 "
            "ORDER BY identity_id LIMIT 10"
        )
    ).fetchall()
    if conflicts:
        samples = ", ".join(str(row[0]) for row in conflicts)
        raise RuntimeError(
            "cannot migrate identity ownership: project relationships have "
            f"multiple owners for {samples}"
        )

    bind.execute(
        sa.text(
            "WITH relationship_candidates AS (" + _RELATIONSHIP_CANDIDATES + "), resolved AS ("
            "SELECT identity_id, min(owner_user_id) AS owner_user_id "
            "FROM relationship_candidates GROUP BY identity_id "
            "HAVING count(DISTINCT owner_user_id) = 1"
            ") UPDATE person_identities AS identity "
            "SET owner_user_id = resolved.owner_user_id FROM resolved "
            "WHERE identity.id = resolved.identity_id "
            "AND identity.owner_user_id IS NULL"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE person_identities AS identity "
            "SET owner_user_id = identity.created_by "
            "WHERE identity.owner_user_id IS NULL "
            "AND identity.created_by IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM users WHERE users.id = identity.created_by)"
        )
    )

    unresolved = bind.execute(
        sa.text("SELECT id FROM person_identities WHERE owner_user_id IS NULL ORDER BY id LIMIT 10")
    ).fetchall()
    if unresolved:
        samples = ", ".join(str(row[0]) for row in unresolved)
        raise RuntimeError(
            f"cannot migrate identity ownership: no deterministic owner for {samples}"
        )

    op.drop_constraint(
        "person_identities_owner_user_id_fkey",
        "person_identities",
        type_="foreignkey",
    )
    op.alter_column(
        "person_identities",
        "owner_user_id",
        existing_type=sa.Text(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_person_identities_owner_user_id_users",
        "person_identities",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_constraint(
        "fk_person_identities_owner_user_id_users",
        "person_identities",
        type_="foreignkey",
    )
    op.alter_column(
        "person_identities",
        "owner_user_id",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.create_foreign_key(
        "person_identities_owner_user_id_fkey",
        "person_identities",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
