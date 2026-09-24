"""Soft-delete for oral avatar / voice clones.

Revision ID: 20260919T1000_oral_soft_delete
Revises: 20260918T1200_publish_account_avatar

人物库里的口播分身与声音克隆此前只能新增、无法移除：克隆出一个不再需要的分身或
声音后，它会永久停在列表里。硬删被 ``oral_tasks`` 的 ``ON DELETE RESTRICT`` 外键
挡住——历史口播视频仍要能追溯它当时用的分身 / 声音，行不能真的消失。因此改为
软删：加 ``deleted_at`` / ``deleted_by_user_id`` 两列，克隆列表与单条读取一律过滤
``deleted_at IS NULL``，行本身保留以满足外键与历史可追溯性。

``deleted_at`` 存 ISO 文本时间戳（与 ``confirmed_at`` 同风格），NULL 表示未删除；
``deleted_by_user_id`` 为纯文本用户 id，与 oral 表其余 ``*_user_id`` 列一致**不加
外键**（065 的 ``owner_user_id`` 也没有），避免改动 ``foreign_keys`` 冻结计数。两列
均可空、无默认值，存量行天然视为未删除。

刻意不建 ``deleted_at IS NULL`` 部分索引：oral_avatars / oral_voices 是按属主 +
人物过滤的小表，065 已有 ``idx_*_identity (identity_id, status)``，再加部分索引对
这种规模没有可测收益，只会平白扩大 schema 冻结面。
"""

from __future__ import annotations

from alembic import op

revision = "20260919T1000_oral_soft_delete"
down_revision = "20260918T1200_publish_account_avatar"
branch_labels = None
depends_on = None

_TABLES = ("oral_avatars", "oral_voices")


def upgrade() -> None:
    # Registered offline archive tools still traverse this chain; runtime is PG-only.
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN deleted_at text")
        op.execute(f"ALTER TABLE {table} ADD COLUMN deleted_by_user_id text")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS deleted_by_user_id")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS deleted_at")
