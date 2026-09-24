from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "014_character_image_generation"
down_revision = "013_character_identity_assets"
branch_labels = None
depends_on = None


def _external_call_fk_name(dialect_name: str) -> str:
    """PostgreSQL caps identifiers at 63 bytes; keep the historical SQLite
    name verbatim so existing SQLite databases stay bit-identical."""
    if dialect_name == "postgresql":
        return "fk_external_call_logs_char_gen_task"
    return "fk_external_call_logs_character_generation_task_id_character_generation_tasks"


def upgrade() -> None:
    with op.batch_alter_table("character_generation_tasks") as batch_op:
        batch_op.add_column(sa.Column("idempotency_key", sa.Text()))
        batch_op.add_column(sa.Column("request_hash", sa.Text()))
        batch_op.add_column(sa.Column("candidate_number", sa.Integer()))
        batch_op.add_column(sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3")
        )
        batch_op.add_column(sa.Column("locked_by", sa.Text()))
        batch_op.add_column(sa.Column("locked_until", sa.Text()))
        batch_op.add_column(sa.Column("next_poll_at", sa.Text()))
        batch_op.add_column(sa.Column("error_message_redacted", sa.Text()))
        batch_op.add_column(sa.Column("created_by", sa.Text()))
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.Text(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "updated_at",
                sa.Text(),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            )
        )

    connection = op.get_bind()
    # SQLite orders ties by the implicit rowid (insertion order). PostgreSQL has
    # no implicit rowid; its closest physical-order equivalent is ctid. On the
    # PG upgrade path this table is freshly created and empty, so the backfill
    # affects zero rows and the tie-breaker only needs to be valid SQL.
    row_tiebreaker = (
        "earlier.ctid <= task.ctid"
        if connection.dialect.name == "postgresql"
        else "earlier.rowid <= task.rowid"
    )
    connection.execute(
        sa.text(
            f"""
            UPDATE character_generation_tasks AS task
            SET idempotency_key = 'legacy:' || task.id,
                request_hash = 'legacy:' || task.id,
                candidate_number = (
                    SELECT COUNT(*)
                    FROM character_generation_tasks AS earlier
                    WHERE earlier.character_version_id = task.character_version_id
                      AND earlier.view_type = task.view_type
                      AND {row_tiebreaker}
                )
            WHERE idempotency_key IS NULL
               OR request_hash IS NULL
               OR candidate_number IS NULL
            """
        )
    )

    with op.batch_alter_table("character_generation_tasks") as batch_op:
        batch_op.alter_column(
            "idempotency_key",
            existing_type=sa.Text(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "request_hash",
            existing_type=sa.Text(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.alter_column(
            "candidate_number",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_character_generation_tasks_candidate_number",
            "candidate_number > 0",
        )
        batch_op.create_check_constraint("ck_character_generation_tasks_attempt", "attempt >= 0")
        batch_op.create_check_constraint(
            "ck_character_generation_tasks_max_attempts",
            "max_attempts > 0",
        )
        batch_op.create_foreign_key(
            "fk_character_generation_tasks_created_by_users",
            "users",
            ["created_by"],
            ["id"],
        )

    op.create_index(
        "idx_character_generation_tasks_lease",
        "character_generation_tasks",
        ["status", "locked_until", "next_poll_at", "created_at"],
    )
    op.create_index(
        "uq_character_generation_tasks_idempotency",
        "character_generation_tasks",
        ["character_version_id", "idempotency_key", "view_type", "candidate_number"],
        unique=True,
    )
    op.create_index(
        "uq_character_generation_tasks_candidate",
        "character_generation_tasks",
        ["character_version_id", "view_type", "candidate_number"],
        unique=True,
    )

    with op.batch_alter_table("external_call_logs") as batch_op:
        batch_op.add_column(sa.Column("character_generation_task_id", sa.Text()))
        batch_op.create_foreign_key(
            _external_call_fk_name(op.get_bind().dialect.name),
            "character_generation_tasks",
            ["character_generation_task_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("external_call_logs") as batch_op:
        batch_op.drop_constraint(
            _external_call_fk_name(op.get_bind().dialect.name),
            type_="foreignkey",
        )
        batch_op.drop_column("character_generation_task_id")

    op.drop_index(
        "uq_character_generation_tasks_candidate",
        table_name="character_generation_tasks",
    )
    op.drop_index(
        "uq_character_generation_tasks_idempotency",
        table_name="character_generation_tasks",
    )
    op.drop_index(
        "idx_character_generation_tasks_lease",
        table_name="character_generation_tasks",
    )
    with op.batch_alter_table("character_generation_tasks") as batch_op:
        batch_op.drop_constraint("ck_character_generation_tasks_max_attempts", type_="check")
        batch_op.drop_constraint("ck_character_generation_tasks_attempt", type_="check")
        batch_op.drop_constraint(
            "ck_character_generation_tasks_candidate_number",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_character_generation_tasks_created_by_users",
            type_="foreignkey",
        )
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("created_by")
        batch_op.drop_column("error_message_redacted")
        batch_op.drop_column("next_poll_at")
        batch_op.drop_column("locked_until")
        batch_op.drop_column("locked_by")
        batch_op.drop_column("max_attempts")
        batch_op.drop_column("attempt")
        batch_op.drop_column("candidate_number")
        batch_op.drop_column("request_hash")
        batch_op.drop_column("idempotency_key")
