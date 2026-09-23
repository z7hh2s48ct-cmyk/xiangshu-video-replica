"""Register content-addressed storage so identical bytes are stored once.

Deduplication needs one authoritative place to ask "do we already hold these
exact bytes, and for whom?".  ``assets.sha256`` cannot answer the ownership
half of that question: a plain ``(sha256, size)`` lookup would let one
customer's upload satisfy another customer's intent, which is exactly the
information leak the existing reference-video reuse guards against.

Hence ``scope``/``scope_owner``.  ``global`` is for media whose content is
public by construction (platform-collected viral videos); ``user`` is for
anything a customer supplied, and its uniqueness is additionally keyed by the
owning user so two customers can never share one row.

Uniqueness is expressed as two partial indexes rather than one UNIQUE
constraint because PostgreSQL treats NULLs as distinct in unique indexes, so a
single constraint keyed on ``scope_owner`` would silently stop deduplicating
``global`` rows.
"""

import sqlalchemy as sa
from alembic import op

revision = "20260916T1400_content_objects"
# Re-parented from 20260914T0000_local_joint_merge: main landed
# 20260915T1600_viral_copy_cache (PR #120) after this branch was cut, and
# alembic refuses to run against a graph with more than one head.
down_revision = "20260915T1200_browser_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Offline history tools may traverse the chain; runtime dedup is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        CREATE TABLE content_objects (
          id            text PRIMARY KEY,
          sha256        text        NOT NULL,
          size_bytes    bigint      NOT NULL CHECK (size_bytes > 0),
          content_type  text,
          provider      text        NOT NULL,
          bucket        text        NOT NULL,
          object_key    text        NOT NULL,
          scope         text        NOT NULL CHECK (scope IN ('user','global')),
          scope_owner   text,
          ref_count     integer     NOT NULL DEFAULT 0 CHECK (ref_count >= 0),
          pinned        boolean     NOT NULL DEFAULT false,
          reclaim_after timestamptz,
          verified_at   timestamptz,
          created_at    timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT ck_content_objects_scope_owner CHECK (
            (scope = 'user' AND scope_owner IS NOT NULL)
            OR (scope = 'global' AND scope_owner IS NULL)
          )
        );
        CREATE UNIQUE INDEX uq_content_objects_user
          ON content_objects (sha256, size_bytes, provider, bucket, scope_owner)
          WHERE scope = 'user';
        CREATE UNIQUE INDEX uq_content_objects_global
          ON content_objects (sha256, size_bytes, provider, bucket)
          WHERE scope = 'global';
        CREATE INDEX idx_content_objects_reclaim
          ON content_objects (reclaim_after)
          WHERE ref_count = 0 AND reclaim_after IS NOT NULL AND NOT pinned;
    """)
    op.add_column(
        "assets",
        sa.Column("content_object_id", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_assets_content_object",
        "assets",
        "content_objects",
        ["content_object_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("idx_assets_sha256", "assets", ["sha256", "size_bytes"])
    op.create_index("idx_assets_content_object", "assets", ["content_object_id"])
    op.create_index(
        "idx_assets_storage_uri",
        "assets",
        ["storage_uri"],
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("idx_assets_storage_uri", table_name="assets")
    op.drop_index("idx_assets_content_object", table_name="assets")
    op.drop_index("idx_assets_sha256", table_name="assets")
    op.drop_constraint("fk_assets_content_object", "assets", type_="foreignkey")
    op.drop_column("assets", "content_object_id")
    op.drop_index("idx_content_objects_reclaim", table_name="content_objects")
    op.drop_index("uq_content_objects_global", table_name="content_objects")
    op.drop_index("uq_content_objects_user", table_name="content_objects")
    op.drop_table("content_objects")
