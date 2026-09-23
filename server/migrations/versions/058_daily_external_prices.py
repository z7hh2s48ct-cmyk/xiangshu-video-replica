"""058_daily_external_prices — 每日对外售价（W8 利润总览收入口径）。

管理员在后台按日录入视频对外生成售价（768P/2K，元→分存储），
作为标准收入的折算口径：日收入 = 当日对外售价 × 当日结算秒数。
价格可覆盖（同日重复录入即改价，UPSERT），历史行保留；
created_by 记录录入人，写路径走管理写契约并落审计。

PostgreSQL-only（客户域约定，027+ 同）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "058_daily_external_prices"
down_revision = "057_second_based_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — 每日售价是客户生产域（027+ 同）。
        return
    op.create_table(
        "daily_external_prices",
        sa.Column("price_date", sa.Date(), nullable=False),
        sa.Column("price_768p_fen", sa.Integer(), nullable=False),
        sa.Column("price_2k_fen", sa.Integer(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("price_date"),
        sa.CheckConstraint("price_768p_fen >= 0", name="ck_daily_price_768p"),
        sa.CheckConstraint("price_2k_fen >= 0", name="ck_daily_price_2k"),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_table("daily_external_prices")
