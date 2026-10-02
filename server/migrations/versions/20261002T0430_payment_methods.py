"""记录经过支付确认核验的方式；历史首选 channel 不作实际付款证据。"""

from alembic import op

revision = "20261002T0430_payment_methods"
down_revision = "20261002T0340_video_collection_outcomes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE recharge_orders ADD COLUMN payment_method text
        CHECK (payment_method IN ('alipay', 'wxpay', 'offline'))
    """)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE recharge_orders DROP COLUMN payment_method")
