"""Allow Xiaohongshu link imports and stored media without enabling discovery."""

import sqlalchemy as sa
from alembic import op

revision = "20260912T1910_xiaohongshu_link_import"
down_revision = "20260912T1900_password_customer_sessions"
branch_labels = None
depends_on = None

_TABLES = (
    "viral_video_favorites",
    "viral_video_visibility",
    "viral_import_tasks",
    "viral_media_preparations",
)


def _replace_constraints(platforms: str) -> None:
    for table in _TABLES:
        constraint = f"ck_{table}_platform"
        op.drop_constraint(constraint, table, type_="check")
        op.create_check_constraint(constraint, table, f"platform IN ({platforms})")


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _replace_constraints("'douyin', 'wechat_channels', 'xiaohongshu'")


def downgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return
    # Preserve imported projects, references and task history on rollback.
    for table in (*_TABLES, "viral_videos"):
        if conn.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE platform = 'xiaohongshu' LIMIT 1")
        ).first():
            raise RuntimeError("Cannot downgrade while Xiaohongshu source records exist")
    _replace_constraints("'douyin', 'wechat_channels'")
