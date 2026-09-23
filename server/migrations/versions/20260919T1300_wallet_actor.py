"""Record the acting user on wallet transactions (子账号消费归属母账号钱包).

Revision ID: 20260919T1300_wallet_actor
Revises: 20260919T1200_sub_accounts

子账号消费统一扣母账号钱包：``wallet_transactions.user_id`` 仍是钱包所有者
（母账号），新增 ``actor_user_id`` 记「实际发起这笔消费的操作者」。

- 母账号自己消费：actor_user_id 可为 NULL（等价于 user_id），或显式写成母账号 id。
- 子账号消费：user_id = 母账号（钱包/额度归属），actor_user_id = 子账号 id。

这样流水表既能按钱包聚合（母账号账单），又能下钻到「哪个子账号花的」，
支撑个人中心「消费流水按子账号聚合/展开」视图（效果图 v5）。

存量行回填 actor_user_id = user_id：子账号体系出现前钱包所有者即实际操作者，
与 Phase 2（T2.10）「母账号消费显式写 actor = user_id」保持一致，避免历史行 NULL /
新行 populated 的永久二分。回填后 NULL 只剩单一语义 =「操作者是已被删除的子账号」。
ON DELETE SET NULL：子账号被删除后消费历史仍保留（归属母账号钱包），仅操作者
attribution 置空，不因删子账号而丢账。nullable 列不影响 ck_wallet_transactions_shape 形状约束。

Runtime is PG-only（与链上相邻迁移同款守卫）。
"""

from __future__ import annotations

from alembic import op

revision = "20260919T1300_wallet_actor"
down_revision = "20260919T1200_sub_accounts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE wallet_transactions
            ADD COLUMN actor_user_id text
                REFERENCES users(id) ON DELETE SET NULL;
        CREATE INDEX idx_wallet_transactions_actor
            ON wallet_transactions (actor_user_id)
            WHERE actor_user_id IS NOT NULL;
    """)
    # 回填存量行：子账号体系出现前，钱包所有者即实际操作者，故 actor = user_id。
    # user_id 本就 FK→users.id，故回填值必为合法 users.id，FK 安全。
    op.execute("UPDATE wallet_transactions SET actor_user_id = user_id WHERE actor_user_id IS NULL")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DROP INDEX IF EXISTS idx_wallet_transactions_actor;
        ALTER TABLE wallet_transactions DROP COLUMN actor_user_id;
    """)
