"""声音克隆：oral_voices 增加样本语言与语速/音量/音调参数。

- ``language``：克隆时提交给上游的样本语言/方言键，默认 ``zh``（普通话）。上游不传
  即按普通话处理，所以存量行回填 ``zh`` 与其真实克隆方式一致。
- ``speech_rate`` / ``volume`` / ``pitch``：当前生效的音色参数，默认 1.0。本系统此前
  从未调用过上游的音色编辑接口，存量声音在上游即为默认值，回填 1.0 如实。

取值范围与上游接口一致（语速 0.5–2.0，音量/音调 0.1–2.0），用一位小数的 NUMERIC
精确存储，界面以 0.1 为步进；CHECK 在库层兜底，任何写入路径都不能落出范围。
语言键同样用 CHECK 约束，新增语言需追加迁移——这是有意的：上游字典变化时要显式
评审，而不是让未知键悄悄流到上游。上游另支持英/日/韩/德/西/法，产品只开放国内语言。

Revision ID: 20260927T0000_oral_voice_language_settings
Revises: 20260927T1200_admin_offline_payment_source
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260927T0000_oral_voice_language_settings"
down_revision = "20260927T1200_admin_offline_payment_source"
branch_labels = None
depends_on = None

_LANGUAGES = (
    "zh",
    "zh_cantonese",
    "zh_sichuanese",
    "zh_shanghainese",
    "zh_tianjinese",
    "zh_zhengzhounese",
    "zh_wuhanese",
)


def upgrade() -> None:
    languages = ", ".join(f"'{language}'" for language in _LANGUAGES)
    # batch 模式：迁移链仍要在 SQLite 旧库导入路径上跑通（SQLite 不支持 ALTER 约束，
    # 会重建表）；在 PostgreSQL 上它退化为与逐条 ALTER 完全相同的语句。
    with op.batch_alter_table("oral_voices") as batch_op:
        batch_op.add_column(sa.Column("language", sa.Text(), nullable=False, server_default="zh"))
        for column in ("speech_rate", "volume", "pitch"):
            batch_op.add_column(
                sa.Column(column, sa.Numeric(2, 1), nullable=False, server_default="1.0")
            )
        batch_op.create_check_constraint("ck_oral_voices_language", f"language IN ({languages})")
        batch_op.create_check_constraint(
            "ck_oral_voices_speech_rate", "speech_rate BETWEEN 0.5 AND 2.0"
        )
        batch_op.create_check_constraint("ck_oral_voices_volume", "volume BETWEEN 0.1 AND 2.0")
        batch_op.create_check_constraint("ck_oral_voices_pitch", "pitch BETWEEN 0.1 AND 2.0")


def downgrade() -> None:
    with op.batch_alter_table("oral_voices") as batch_op:
        for name in (
            "ck_oral_voices_pitch",
            "ck_oral_voices_volume",
            "ck_oral_voices_speech_rate",
            "ck_oral_voices_language",
        ):
            batch_op.drop_constraint(name, type_="check")
        for column in ("pitch", "volume", "speech_rate", "language"):
            batch_op.drop_column(column)
