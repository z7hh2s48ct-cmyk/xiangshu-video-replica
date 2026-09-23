"""W10 — operation cost rates and external per-second prices (费率配置底座).

管理后台「系统设置 · 费率管理」的持久层：上游成本费率（对标秘塔 H3
按秒/按分辨率计价，含参考视频秒费与按次科目）与对外售价（按秒）。
每科目一行、可更新、更新人与更新时间落行；历史变更不在此表——每次
调整写 audit_logs（old/new + reason + request_id），由
``GET /api/control/settings/rates`` 的 history 段读取。

Seed defaults mirror the Metaso H3 price list registered in
``docs/METASO-H3查询真实响应样本.md``（768P 0.09 元/秒、2K 0.15 元/秒，
对外售价默认 0.12 / 0.20 元/秒）。Seed rows are operator-adjustable data,
not code constants: the migration only guarantees the subjects exist.

PostgreSQL-only（客户域约定，027+ 同）；SQLite 内部桌面通道不消费费率。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "056_operation_cost_rates"
down_revision = "055_customer_batch_visibility"
branch_labels = None
depends_on = None

# (subject, kind, unit, resolution, unit_price_fen, label)
_RATE_SEEDS = [
    ("video_generation_768p", "upstream_cost", "second", "768P", 9),
    ("video_generation_2k", "upstream_cost", "second", "2K", 15),
    ("video_analysis_768p", "upstream_cost", "second", "768P", 9),
    ("video_analysis_2k", "upstream_cost", "second", "2K", 15),
    ("first_frame_image", "upstream_cost", "image", None, 5),
    ("character_sheet_image", "upstream_cost", "image", None, 5),
    ("context_ir", "upstream_cost", "call", None, 5),
    ("external_price_768p", "external_price", "second", "768P", 12),
    ("external_price_2k", "external_price", "second", "2K", 20),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite: internal P0 runtime — 费率管理是客户生产域（027+ 同）。
        return
    op.create_table(
        "operation_cost_rates",
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column(
            "kind",
            sa.String(),
            nullable=False,
            comment="upstream_cost（上游成本）或 external_price（对外售价）",
        ),
        sa.Column(
            "unit",
            sa.String(),
            nullable=False,
            comment="计价单位：second（秒）/ image（张）/ call（次）",
        ),
        sa.Column("resolution", sa.String(), nullable=True),
        sa.Column("unit_price_fen", sa.Integer(), nullable=False),
        sa.Column("updated_by_user_id", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("subject"),
        sa.CheckConstraint("kind IN ('upstream_cost', 'external_price')", name="ck_rate_kind"),
        sa.CheckConstraint("unit IN ('second', 'image', 'call')", name="ck_rate_unit"),
        sa.CheckConstraint("unit_price_fen >= 0", name="ck_rate_price_non_negative"),
    )
    op.create_index("idx_operation_cost_rates_kind", "operation_cost_rates", ["kind"])
    for subject, kind, unit, resolution, price in _RATE_SEEDS:
        op.execute(
            sa.text(
                """
                INSERT INTO operation_cost_rates
                    (subject, kind, unit, resolution, unit_price_fen, updated_at)
                VALUES (:subject, :kind, :unit, :resolution, :price, now())
                ON CONFLICT (subject) DO NOTHING
                """
            ).bindparams(
                subject=subject,
                kind=kind,
                unit=unit,
                resolution=resolution,
                price=price,
            )
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_index("idx_operation_cost_rates_kind", table_name="operation_cost_rates")
    op.drop_table("operation_cost_rates")
