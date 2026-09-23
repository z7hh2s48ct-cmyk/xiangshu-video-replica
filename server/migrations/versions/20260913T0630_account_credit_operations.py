"""Account credit operations and audited legacy balance conversion."""

import sqlalchemy as sa
from alembic import op

revision = "20260913T0630_account_credit_operations"
down_revision = "20260912T2330_customer_credit_pricing"
branch_labels = None
depends_on = None

_OLD_SHAPE = """
(type = 'CHARGE' AND available_delta > 0 AND reserved_delta = 0
 AND recharge_order_id IS NOT NULL AND task_id IS NULL AND oral_task_id IS NULL
 AND billing_round IS NULL) OR
(type = 'RESERVE' AND available_delta = -reserved_delta AND reserved_delta >= 1
 AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL))
 AND billing_round IS NOT NULL) OR
(type = 'SETTLE' AND available_delta = 0 AND reserved_delta <= -1
 AND recharge_order_id IS NULL AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL))
 AND billing_round IS NOT NULL) OR
(type = 'RELEASE' AND available_delta = -reserved_delta AND available_delta >= 1
 AND reserved_delta <= -1 AND recharge_order_id IS NULL
 AND ((task_id IS NOT NULL) <> (oral_task_id IS NOT NULL)) AND billing_round IS NOT NULL)
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.drop_constraint("ck_admin_adjustments_source_type", "admin_adjustments", type_="check")
    op.create_check_constraint(
        "ck_admin_adjustments_source_type",
        "admin_adjustments",
        "source_document_type IN ('CS_TICKET', 'REFUND_APPROVAL', 'COMPENSATION_APPROVAL', "
        "'LEDGER_CORRECTION', 'FREE_GRANT', 'CREDIT_COMPENSATION')",
    )
    op.execute("""
        CREATE TABLE legacy_credit_policy (
          id integer PRIMARY KEY CHECK(id = 1),
          version integer NOT NULL DEFAULT 0 CHECK(version >= 0),
          mode text NOT NULL DEFAULT 'keep' CHECK(mode IN ('keep', 'convert')),
          numerator integer NOT NULL DEFAULT 1 CHECK(numerator BETWEEN 1 AND 1000000),
          denominator integer NOT NULL DEFAULT 1 CHECK(denominator BETWEEN 1 AND 1000000)
        );
        INSERT INTO legacy_credit_policy (id) VALUES (1);
        CREATE TABLE wallet_credit_conversions (
          user_id text PRIMARY KEY REFERENCES users(id),
          before_credits integer NOT NULL CHECK(before_credits >= 0),
          after_credits integer NOT NULL CHECK(after_credits >= 0),
          policy_version integer NOT NULL CHECK(policy_version >= 0),
          mode text NOT NULL CHECK(mode IN ('keep', 'convert')),
          numerator integer NOT NULL CHECK(numerator BETWEEN 1 AND 1000000),
          denominator integer NOT NULL CHECK(denominator BETWEEN 1 AND 1000000),
          ledger_id text UNIQUE REFERENCES wallet_transactions(id),
          actor_user_id text NOT NULL REFERENCES users(id),
          reason text NOT NULL CHECK(length(trim(reason)) > 0),
          created_at timestamptz NOT NULL DEFAULT now(),
          CHECK(after_credits::bigint = CASE WHEN mode = 'keep' THEN before_credits::bigint
                ELSE before_credits::bigint * numerator / denominator END),
          CHECK((before_credits = after_credits) = (ledger_id IS NULL))
        );
        CREATE TRIGGER trg_credit_conversion_append_only BEFORE UPDATE OR DELETE
          ON wallet_credit_conversions FOR EACH ROW
          EXECUTE FUNCTION admin_adjustments_refuse_rewrite();
        CREATE TRIGGER trg_credit_conversion_no_truncate BEFORE TRUNCATE
          ON wallet_credit_conversions FOR EACH STATEMENT
          EXECUTE FUNCTION refuse_truncate_of_audit_tables();
    """)
    op.drop_constraint("ck_wallet_transactions_type", "wallet_transactions", type_="check")
    op.create_check_constraint(
        "ck_wallet_transactions_type",
        "wallet_transactions",
        "type IN ('CHARGE', 'RESERVE', 'SETTLE', 'RELEASE', 'CONVERSION')",
    )
    op.drop_constraint("ck_wallet_transactions_shape", "wallet_transactions", type_="check")
    op.create_check_constraint(
        "ck_wallet_transactions_shape",
        "wallet_transactions",
        _OLD_SHAPE + " OR (type = 'CONVERSION' AND available_delta <> 0 AND reserved_delta = 0 "
        "AND recharge_order_id IS NULL AND task_id IS NULL AND "
        "oral_task_id IS NULL AND billing_round IS NULL)",
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM wallet_credit_conversions) "
                "OR EXISTS(SELECT 1 FROM legacy_credit_policy WHERE version > 0) "
                "OR EXISTS(SELECT 1 FROM wallet_transactions WHERE type = 'CONVERSION')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Credit conversion settings and history must survive rollback.")
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS(SELECT 1 FROM admin_adjustments WHERE "
                "source_document_type = 'CREDIT_COMPENSATION')"
            )
        )
        .scalar()
    ):
        raise RuntimeError("Credit compensation history must survive rollback.")
    op.drop_table("wallet_credit_conversions")
    op.drop_table("legacy_credit_policy")
    op.drop_constraint("ck_wallet_transactions_type", "wallet_transactions", type_="check")
    op.create_check_constraint(
        "ck_wallet_transactions_type",
        "wallet_transactions",
        "type IN ('CHARGE', 'RESERVE', 'SETTLE', 'RELEASE')",
    )
    op.drop_constraint("ck_wallet_transactions_shape", "wallet_transactions", type_="check")
    op.create_check_constraint("ck_wallet_transactions_shape", "wallet_transactions", _OLD_SHAPE)
    op.drop_constraint("ck_admin_adjustments_source_type", "admin_adjustments", type_="check")
    op.create_check_constraint(
        "ck_admin_adjustments_source_type",
        "admin_adjustments",
        "source_document_type IN ('CS_TICKET', 'REFUND_APPROVAL', 'COMPENSATION_APPROVAL', "
        "'LEDGER_CORRECTION', 'FREE_GRANT')",
    )
