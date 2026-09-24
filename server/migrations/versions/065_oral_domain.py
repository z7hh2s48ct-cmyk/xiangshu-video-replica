"""C1 数字人口播领域：分身 / 声音 / 口播任务。

Vendor-neutral naming on purpose: tables never mention the upstream vendor;
vendor ids are opaque strings. Wallet billing for oral tasks deliberately
waits for a dedicated slice — the internal-billing reconciler (BILL-03) is
generation-task scoped, so wiring RESERVE/SETTLE here without a task-type
discriminator would let it auto-release oral reservations.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "065_oral_domain"
down_revision = "064_hifly_provider"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column[str]]:
    return [
        sa.Column(
            "created_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.Text(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "oral_avatars",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("vendor_avatar_id", sa.Text()),
        sa.Column("vendor_task_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("source_asset_id", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'READY', 'FAILED')",
            name="ck_oral_avatars_status",
        ),
        sa.CheckConstraint(
            "source_kind IN ('VIDEO', 'IMAGE')",
            name="ck_oral_avatars_source_kind",
        ),
    )
    op.create_index(
        "idx_oral_avatars_identity",
        "oral_avatars",
        ["identity_id", "status"],
    )

    op.create_table(
        "oral_voices",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("vendor_voice_id", sa.Text()),
        sa.Column("vendor_task_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("source_asset_id", sa.Text(), nullable=False),
        sa.Column("demo_asset_id", sa.Text()),
        sa.Column("confirmed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'READY', 'FAILED')",
            name="ck_oral_voices_status",
        ),
        sa.CheckConstraint("confirmed IN (0, 1)", name="ck_oral_voices_confirmed"),
    )
    op.create_index(
        "idx_oral_voices_identity",
        "oral_voices",
        ["identity_id", "status"],
    )

    op.create_table(
        "oral_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner_user_id", sa.Text(), nullable=False),
        sa.Column(
            "identity_id",
            sa.Text(),
            sa.ForeignKey("person_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "avatar_id",
            sa.Text(),
            sa.ForeignKey("oral_avatars.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "voice_id",
            sa.Text(),
            sa.ForeignKey("oral_voices.id", ondelete="RESTRICT"),
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("script_text", sa.Text()),
        sa.Column("audio_asset_id", sa.Text()),
        sa.Column("subtitle_json", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="QUEUED"),
        sa.Column("vendor_task_id", sa.Text()),
        sa.Column("result_asset_id", sa.Text()),
        sa.Column("duration_sec", sa.Integer()),
        sa.Column("estimated_cost_fen", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_oral_tasks_idempotency_key"),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_oral_tasks_status",
        ),
        sa.CheckConstraint("mode IN ('TTS', 'AUDIO')", name="ck_oral_tasks_mode"),
    )
    op.create_index(
        "idx_oral_tasks_owner",
        "oral_tasks",
        ["owner_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("oral_tasks")
    op.drop_table("oral_voices")
    op.drop_table("oral_avatars")
