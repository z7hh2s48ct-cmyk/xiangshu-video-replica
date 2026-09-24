"""Require auditable consent for avatar and voice cloning.

Revision ID: 072_oral_clone_consent
Revises: 071_studio_material_preferences
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "072_oral_clone_consent"
down_revision = "071_studio_material_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "oral_consents",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "owner_user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_asset_id",
            sa.Text(),
            sa.ForeignKey("assets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("consent_text_version", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("consented_at", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "purpose IN ('AVATAR_CLONE', 'VOICE_CLONE')",
            name="ck_oral_consents_purpose",
        ),
        sa.CheckConstraint(
            "length(consent_text_version) > 0",
            name="ck_oral_consents_text_version",
        ),
        sa.CheckConstraint(
            "length(source_sha256) > 0",
            name="ck_oral_consents_source_sha256",
        ),
    )
    op.create_index(
        "idx_oral_consents_owner_identity",
        "oral_consents",
        ["owner_user_id", "identity_id", "consented_at"],
    )

    with op.batch_alter_table("oral_avatars") as batch_op:
        batch_op.add_column(sa.Column("consent_id", sa.Text()))
        batch_op.add_column(sa.Column("idempotency_key", sa.Text()))
        batch_op.add_column(sa.Column("request_hash", sa.Text()))
        batch_op.add_column(
            sa.Column(
                "submission_state",
                sa.Text(),
                nullable=False,
                server_default="SUBMITTED",
            )
        )
        batch_op.create_foreign_key(
            "fk_oral_avatars_consent_id",
            "oral_consents",
            ["consent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint(
            "uq_oral_avatars_owner_idempotency",
            ["owner_user_id", "idempotency_key"],
        )
        batch_op.create_check_constraint(
            "ck_oral_avatars_submission_state",
            "submission_state IN ('LOCAL_PENDING', 'SUBMITTED', 'SUBMISSION_UNKNOWN', 'FAILED')",
        )
    with op.batch_alter_table("oral_voices") as batch_op:
        batch_op.add_column(sa.Column("consent_id", sa.Text()))
        batch_op.add_column(sa.Column("idempotency_key", sa.Text()))
        batch_op.add_column(sa.Column("request_hash", sa.Text()))
        batch_op.add_column(
            sa.Column(
                "submission_state",
                sa.Text(),
                nullable=False,
                server_default="SUBMITTED",
            )
        )
        batch_op.add_column(sa.Column("confirmed_by_user_id", sa.Text()))
        batch_op.add_column(sa.Column("confirmed_at", sa.Text()))
        batch_op.create_foreign_key(
            "fk_oral_voices_consent_id",
            "oral_consents",
            ["consent_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_oral_voices_confirmed_by_user_id",
            "users",
            ["confirmed_by_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_unique_constraint(
            "uq_oral_voices_owner_idempotency",
            ["owner_user_id", "idempotency_key"],
        )
        batch_op.create_check_constraint(
            "ck_oral_voices_submission_state",
            "submission_state IN ('LOCAL_PENDING', 'SUBMITTED', 'SUBMISSION_UNKNOWN', 'FAILED')",
        )
    with op.batch_alter_table("oral_tasks") as batch_op:
        batch_op.add_column(sa.Column("request_hash", sa.Text()))
        batch_op.add_column(
            sa.Column(
                "submission_state",
                sa.Text(),
                nullable=False,
                server_default="SUBMITTED",
            )
        )
        batch_op.drop_constraint("uq_oral_tasks_idempotency_key", type_="unique")
        batch_op.create_unique_constraint(
            "uq_oral_tasks_owner_idempotency",
            ["owner_user_id", "idempotency_key"],
        )
        batch_op.create_check_constraint(
            "ck_oral_tasks_submission_state",
            "submission_state IN ('LOCAL_PENDING', 'SUBMITTED', 'SUBMISSION_UNKNOWN', 'FAILED')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    duplicate_key = bind.execute(
        sa.text(
            """
            SELECT idempotency_key
            FROM oral_tasks
            WHERE idempotency_key IS NOT NULL
            GROUP BY idempotency_key
            HAVING COUNT(*) > 1
            LIMIT 1
            """
        )
    ).first()
    if duplicate_key is not None:
        raise RuntimeError(
            "cannot downgrade 064 while owner-scoped oral task idempotency keys overlap"
        )

    with op.batch_alter_table("oral_tasks") as batch_op:
        batch_op.drop_constraint("ck_oral_tasks_submission_state", type_="check")
        batch_op.drop_constraint("uq_oral_tasks_owner_idempotency", type_="unique")
        batch_op.create_unique_constraint("uq_oral_tasks_idempotency_key", ["idempotency_key"])
        batch_op.drop_column("submission_state")
        batch_op.drop_column("request_hash")
    with op.batch_alter_table("oral_voices") as batch_op:
        batch_op.drop_constraint("ck_oral_voices_submission_state", type_="check")
        batch_op.drop_constraint("uq_oral_voices_owner_idempotency", type_="unique")
        batch_op.drop_constraint("fk_oral_voices_confirmed_by_user_id", type_="foreignkey")
        batch_op.drop_constraint("fk_oral_voices_consent_id", type_="foreignkey")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("confirmed_by_user_id")
        batch_op.drop_column("submission_state")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("consent_id")
    with op.batch_alter_table("oral_avatars") as batch_op:
        batch_op.drop_constraint("ck_oral_avatars_submission_state", type_="check")
        batch_op.drop_constraint("uq_oral_avatars_owner_idempotency", type_="unique")
        batch_op.drop_constraint("fk_oral_avatars_consent_id", type_="foreignkey")
        batch_op.drop_column("submission_state")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("consent_id")
    op.drop_index("idx_oral_consents_owner_identity", table_name="oral_consents")
    op.drop_table("oral_consents")
