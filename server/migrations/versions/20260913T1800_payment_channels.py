"""Administrator-selected payment provider and asynchronous Native QR creation."""

from alembic import op

revision = "20260913T1800_payment_channels"
down_revision = "20260913T1100_itemized_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        ALTER TABLE runtime_settings ADD COLUMN active_payment_provider text NOT NULL
          DEFAULT 'zpay' CHECK (active_payment_provider IN ('zpay', 'wechat_native'));
        ALTER TABLE provider_settings DROP CONSTRAINT ck_provider_settings_supported_provider;
        ALTER TABLE provider_settings ADD CONSTRAINT ck_provider_settings_supported_provider
          CHECK (provider IN ('apilio','metaso','cos','deepseek','zpay','hifly',
            'tikhub','dashscope','douyidou','wechat_native'));
        ALTER TABLE recharge_orders DROP CONSTRAINT ck_recharge_orders_wechat_prepay_id;
        ALTER TABLE recharge_orders DROP CONSTRAINT ck_recharge_orders_wechat_code_url;
        ALTER TABLE recharge_orders ADD CONSTRAINT ck_recharge_orders_wechat_prepay_id
          CHECK (provider = 'wechat_native' OR prepay_id IS NULL);
        ALTER TABLE recharge_orders ADD CONSTRAINT ck_recharge_orders_wechat_code_url
          CHECK (provider = 'wechat_native' OR code_url IS NULL);
    """)
    # Native V3 returns code_url, not prepay_id. QR creation happens after the
    # local PENDING order commits, so neither value may be required at insertion.


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM recharge_orders WHERE provider='wechat_native')
            OR EXISTS (SELECT 1 FROM provider_settings WHERE provider='wechat_native')
            OR EXISTS (SELECT 1 FROM runtime_settings WHERE active_payment_provider='wechat_native')
          THEN RAISE EXCEPTION 'Payment data exists; refusing destructive downgrade'; END IF;
        END $$;
        ALTER TABLE runtime_settings DROP COLUMN active_payment_provider;
        ALTER TABLE provider_settings DROP CONSTRAINT ck_provider_settings_supported_provider;
        ALTER TABLE provider_settings ADD CONSTRAINT ck_provider_settings_supported_provider
          CHECK (provider IN ('apilio','metaso','cos','deepseek','zpay','hifly',
            'tikhub','dashscope','douyidou'));
        ALTER TABLE recharge_orders DROP CONSTRAINT ck_recharge_orders_wechat_prepay_id;
        ALTER TABLE recharge_orders DROP CONSTRAINT ck_recharge_orders_wechat_code_url;
        ALTER TABLE recharge_orders ADD CONSTRAINT ck_recharge_orders_wechat_prepay_id
          CHECK ((provider='wechat_native' AND prepay_id IS NOT NULL)
            OR (provider!='wechat_native' AND prepay_id IS NULL));
        ALTER TABLE recharge_orders ADD CONSTRAINT ck_recharge_orders_wechat_code_url
          CHECK ((provider='wechat_native' AND code_url IS NOT NULL)
            OR (provider!='wechat_native' AND code_url IS NULL));
    """)
