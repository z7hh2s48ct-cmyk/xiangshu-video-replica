from __future__ import annotations

from alembic import op

revision = "009_idempotency_project_scope"
down_revision = "008_provider_result_url"
branch_labels = None
depends_on = None

_GENERATION_BATCHES_NEW = """
CREATE TABLE generation_batches_new (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT uq_generation_batches_user_project_key
        UNIQUE (created_by_user_id, project_id, idempotency_key),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)
"""

_GENERATION_BATCHES_OLD = """
CREATE TABLE generation_batches_new (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    created_by_user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE (created_by_user_id, idempotency_key),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id)
)
"""


def _rebuild(conn, *, create_sql: str) -> None:
    conn.exec_driver_sql(create_sql)
    conn.exec_driver_sql(
        """
        INSERT INTO generation_batches_new (
            id, project_id, created_by_user_id, idempotency_key,
            request_hash, request_snapshot_json, status, created_at, updated_at
        )
        SELECT id, project_id, created_by_user_id, idempotency_key,
               request_hash, request_snapshot_json, status, created_at, updated_at
        FROM generation_batches
        """
    )
    # PostgreSQL enforces the generation_tasks FK that SQLite ignores, so the
    # dependent constraint must be detached before the swap and re-attached
    # afterwards. SQLite keeps its historical single-step path untouched.
    is_postgresql = conn.dialect.name == "postgresql"
    if is_postgresql:
        conn.exec_driver_sql(
            "ALTER TABLE generation_tasks DROP CONSTRAINT generation_tasks_batch_id_fkey"
        )
    conn.exec_driver_sql("DROP TABLE generation_batches")
    conn.exec_driver_sql("ALTER TABLE generation_batches_new RENAME TO generation_batches")
    if is_postgresql:
        conn.exec_driver_sql(
            "ALTER TABLE generation_tasks ADD CONSTRAINT generation_tasks_batch_id_fkey "
            "FOREIGN KEY (batch_id) REFERENCES generation_batches (id) ON DELETE CASCADE"
        )


def upgrade() -> None:
    """Scope the idempotency key per (user, project) so a key reused across
    projects no longer silently returns another project's batch."""
    _rebuild(op.get_bind(), create_sql=_GENERATION_BATCHES_NEW)


def downgrade() -> None:
    _rebuild(op.get_bind(), create_sql=_GENERATION_BATCHES_OLD)
