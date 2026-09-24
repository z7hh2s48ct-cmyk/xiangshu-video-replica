"""T25 — user fair queue (per-user concurrency 1, round-robin rotation).

This revision was originally named ``030_user_fair_queue`` chaining off 029,
with 032 re-pointed to keep the graph linear. M5 review (P1-3) found that
re-pointing a published revision breaks upgrades of databases already stamped
032–040: 030 would never be applied there (it is not on their ancestor path),
so the fair-queue schema silently stays absent while the runtime queries it.
The fix is append-only (031's comment documents the precedent): 032 is
restored to chain off 029 exactly as published, and this revision moves to
the tail as **041_user_fair_queue** descending from 040 (the released head).
The frozen 030 file-name slot (AGENTS.md / task-list red line) is left empty;
the revision id is renamed because the file is still part of this unpublished
branch. Existing databases now apply 041 as a normal upgrade; fresh databases
apply it after 040. The DDL is unchanged from the 029-based design — it only
depends on tables created well before 040.

Design (revised ADR §1, §2):
- ``user_queue_cursors`` holds one row per user; ``running_tasks_count`` is
  CHECK-constrained to {0, 1} (per-user concurrency 1). Columns are PG-native
  TIMESTAMPTZ (this table is PostgreSQL-only, unlike the TEXT timestamp
  columns of legacy tables).
- The partial index serves the rotation hot path: idle users ordered by
  least-recently-dispatched.
- The BEFORE UPDATE trigger keeps ``updated_at`` honest; the legacy 001
  migration only defines the column default, not the trigger.
- ``runtime_settings.fair_queue_enabled`` gates the queue globally (revised
  ADR §4: a runtime switch, not a per-row column); FALSE keeps the legacy
  global FIFO so the rollout can be observed before enabling.
- The seed derives each user's initial cursor from ``generation_batches``
  (task → batch → created_by_user_id is the real ownership chain; revised
  ADR §3 — no JOIN against activation_codes, which reference a different
  batches table). A legacy running count above 1 is clamped to 1 with
  LEAST so the migration cannot fail on pre-existing data that already
  violated the per-user invariant (revised ADR §3 "先处置再种" is the
  operational ideal; a failed migration is strictly worse).

PostgreSQL only (031 precedent): the desktop SQLite lane keeps its legacy
global FIFO worker and never grows this table.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "041_user_fair_queue"
down_revision = "040_fix_provider_settings_constraint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — the fair queue is customer-production
        # only; the legacy global FIFO worker stays there.
        return
    op.add_column(
        "runtime_settings",
        sa.Column(
            "fair_queue_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    op.create_table(
        "user_queue_cursors",
        sa.Column(
            "user_id",
            sa.Text(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "last_dispatched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "running_tasks_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "running_tasks_count >= 0 AND running_tasks_count <= 1",
            name="valid_running_tasks",
        ),
    )
    # Rotation hot path: idle users ordered by least-recently-dispatched.
    op.create_index(
        "idx_user_queue_cursors_rotation",
        "user_queue_cursors",
        ["last_dispatched_at"],
        postgresql_where=sa.text("running_tasks_count = 0"),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION update_updated_at_column()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = now();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_user_queue_cursors_updated_at
            BEFORE UPDATE ON user_queue_cursors
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column()
        """
    )
    # Seed from the real ownership chain (revised ADR §3 Step 2). Clamp any
    # legacy per-user running count above 1 so the migration never fails on
    # data that already violated the invariant.
    op.execute(
        """
        INSERT INTO user_queue_cursors (
            user_id, last_dispatched_at, running_tasks_count
        )
        SELECT
            b.created_by_user_id,
            COALESCE(MAX(t.created_at::timestamptz), now()),
            LEAST(
                SUM(CASE WHEN t.status IN ('SUBMITTING', 'RUNNING', 'ARCHIVING')
                         THEN 1 ELSE 0 END),
                1
            )
        FROM generation_batches b
        LEFT JOIN generation_tasks t ON t.batch_id = b.id
        GROUP BY b.created_by_user_id
        ON CONFLICT (user_id) DO UPDATE SET
            last_dispatched_at = EXCLUDED.last_dispatched_at,
            running_tasks_count = EXCLUDED.running_tasks_count
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP TRIGGER IF EXISTS trg_user_queue_cursors_updated_at ON user_queue_cursors")
    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column()")
    op.drop_index("idx_user_queue_cursors_rotation", table_name="user_queue_cursors")
    op.drop_table("user_queue_cursors")
    op.drop_column("runtime_settings", "fair_queue_enabled")
