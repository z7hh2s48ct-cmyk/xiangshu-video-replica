from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_settings"
down_revision = "001_core"
branch_labels = None
depends_on = None


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
    op.create_table(
        "provider_settings",
        sa.Column("provider", sa.Text(), primary_key=True),
        sa.Column("encrypted_config", sa.Text(), nullable=False),
        sa.Column("updated_by_user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("provider IN ('apilio', 'metaso', 'cos', 'oss')"),
    )
    op.create_table(
        "runtime_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "max_generation_count_per_batch",
            sa.Integer(),
            nullable=False,
            server_default="4",
        ),
        sa.Column(
            "max_concurrent_h3_tasks",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column("updated_by_user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint("id = 1"),
        sa.CheckConstraint("max_generation_count_per_batch > 0"),
        sa.CheckConstraint("max_concurrent_h3_tasks > 0"),
    )
    op.execute(
        """
        INSERT INTO runtime_settings (
            id,
            max_generation_count_per_batch,
            max_concurrent_h3_tasks
        )
        VALUES (1, 4, 2)
        """
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
    op.drop_table("provider_settings")
