"""075_independent_creation — 独立视频创作通道（C2）.

generation_batches.project_id 改为可空：独立创作（文生/图生/参考生）
不属于任何项目，批次以 created_by_user_id 为归属。已有的复刻流批次
project_id 全部非空，语义不变。

runtime_settings.h3_extended_modes_enabled 是扩展模式（尾帧 / 文生 T2V /
参考生 R2V）真实提交的总开关：协议构造与测试先行落地，真实供应商提交
在按 docs/短视频复刻桌面端开发说明.md §21.3 完成供应商核对后才由管理端
打开。首帧图生（I2V）不受此开关影响。

Revision ID: 075_independent_creation
Revises: 074_script_rewrite_ip_profile_snapshot
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "075_independent_creation"
down_revision = "074_script_rewrite_ip_profile_snapshot"
branch_labels = None
depends_on = None

_INDEPENDENT_LIST_INDEX = "idx_generation_batches_independent_list"
_INDEPENDENT_IDEMPOTENCY_INDEX = "uq_generation_batches_independent_idempotency"

# 063_wallet_ledger_sequence attached these triggers to wallet_transactions.
# SQLite's batch_alter_table rebuild drops them with the old table, so they
# must be restored after every rebuild in both directions.
_SQLITE_LEDGER_TRIGGERS = (
    (
        "trg_wallet_transactions_reject_explicit_ledger_sequence",
        """
        BEFORE INSERT ON wallet_transactions
        WHEN NEW.ledger_sequence IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'ledger_sequence is database assigned');
        END
        """,
    ),
    (
        "trg_wallet_transactions_reject_ledger_sequence_update",
        """
        BEFORE UPDATE OF ledger_sequence ON wallet_transactions
        WHEN OLD.ledger_sequence IS NOT NULL
             AND NEW.ledger_sequence IS NOT OLD.ledger_sequence
        BEGIN
            SELECT RAISE(ABORT, 'ledger_sequence is immutable');
        END
        """,
    ),
    (
        "trg_wallet_transactions_assign_ledger_sequence",
        """
        AFTER INSERT ON wallet_transactions
        WHEN NEW.ledger_sequence IS NULL
        BEGIN
            UPDATE wallet_transactions
            SET ledger_sequence = (
                SELECT COALESCE(MAX(ledger_sequence), 0) + 1
                FROM wallet_transactions
                WHERE id <> NEW.id
            )
            WHERE id = NEW.id;
        END
        """,
    ),
)


def _restore_sqlite_ledger_triggers() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    for name, body in _SQLITE_LEDGER_TRIGGERS:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
        op.execute(sa.text(f"CREATE TRIGGER {name}{body}"))


def upgrade() -> None:
    with op.batch_alter_table("generation_batches") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.Text(),
            existing_nullable=False,
            nullable=True,
        )
    op.create_index(
        _INDEPENDENT_LIST_INDEX,
        "generation_batches",
        ["created_by_user_id", "created_at"],
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )
    # NULL project_id 在 SQL 唯一约束下互不相等：独立批次的幂等键必须由
    # 部分唯一索引保护，否则 PG 并发重复提交会双建批、双预留。
    op.create_index(
        _INDEPENDENT_IDEMPOTENCY_INDEX,
        "generation_batches",
        ["created_by_user_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )
    op.add_column(
        "runtime_settings",
        sa.Column(
            "h3_extended_modes_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
    )
    _rebuild_wallet_shape_check()


def _rebuild_wallet_shape_check() -> None:
    """把 073 的钱包形状 CHECK 重建为按秒放宽语义。

    073 首次发布时把 RESERVE/SETTLE/RELEASE 写死为 ±1；057（按秒计费）
    依赖 -seconds/+seconds 形状。已在旧 073 上迁移过的库不会重跑迁移，
    这里统一重建一次，保证两条计费模型在任意环境都可写流水。
    """
    with_shape = (
        "(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0 "
        "AND recharge_order_id IS NOT NULL AND task_id IS NULL AND oral_task_id IS NULL "
        "AND billing_round IS NULL) OR "
        "(type = 'RESERVE' AND available_delta = -reserved_delta AND reserved_delta >= 1 "
        "AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL)) "
        "AND billing_round IS NOT NULL) OR "
        "(type = 'SETTLE' AND available_delta = 0 AND reserved_delta <= -1 "
        "AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL)) "
        "AND billing_round IS NOT NULL) OR "
        "(type = 'RELEASE' AND available_delta = -reserved_delta AND available_delta >= 1 "
        "AND reserved_delta <= -1 "
        "AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL)) "
        "AND billing_round IS NOT NULL)"
    )
    with op.batch_alter_table("wallet_transactions") as batch_op:
        batch_op.drop_constraint("ck_wallet_transactions_shape", type_="check")
        batch_op.create_check_constraint("ck_wallet_transactions_shape", with_shape)
    _restore_sqlite_ledger_triggers()


def downgrade() -> None:
    op.drop_column("runtime_settings", "h3_extended_modes_enabled")
    op.drop_index(_INDEPENDENT_IDEMPOTENCY_INDEX, table_name="generation_batches")
    op.drop_index(_INDEPENDENT_LIST_INDEX, table_name="generation_batches")
    connection = op.get_bind()
    orphan_batches = connection.execute(
        sa.text("SELECT 1 FROM generation_batches WHERE project_id IS NULL LIMIT 1")
    ).fetchone()
    if orphan_batches is not None:
        raise RuntimeError(
            "cannot downgrade 075 while independent generation batches exist; "
            "export or delete them first"
        )
    with op.batch_alter_table("generation_batches") as batch_op:
        batch_op.alter_column(
            "project_id",
            existing_type=sa.Text(),
            existing_nullable=True,
            nullable=False,
        )
