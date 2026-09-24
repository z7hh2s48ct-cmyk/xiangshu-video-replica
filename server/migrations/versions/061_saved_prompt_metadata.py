"""061_saved_prompt_metadata — 提示词范围、来源与作者元数据。

个人提示词库复用 versions 版本底座。独立列用于所有权过滤与审计查询，
正文仍保存在 payload_json，避免引入第二套提示词存储。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "061_saved_prompt_metadata"
down_revision = "059_operation_cost_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("versions") as batch_op:
        batch_op.add_column(sa.Column("scope", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("source", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("author_user_id", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_versions_author_user_id_users",
            "users",
            ["author_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_versions_saved_prompt_owner",
            "kind <> 'saved_prompt' OR ("
            "scope IS NOT NULL AND scope = 'user' AND "
            "source IS NOT NULL AND author_user_id IS NOT NULL)",
        )
    op.create_index(
        "idx_versions_saved_prompt_owner",
        "versions",
        ["project_id", "kind", "author_user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_versions_saved_prompt_owner", table_name="versions")
    with op.batch_alter_table("versions") as batch_op:
        batch_op.drop_constraint(
            "ck_versions_saved_prompt_owner",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_versions_author_user_id_users",
            type_="foreignkey",
        )
        batch_op.drop_column("author_user_id")
        batch_op.drop_column("source")
        batch_op.drop_column("scope")
