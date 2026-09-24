"""083_recharge_orders_multi_provider — widen recharge_orders for WeChat Native.

CW-067: Add ``wechat_native`` to the provider enum and the three WeChat-specific
columns (``prepay_id``, ``code_url``, ``transaction_id``).  All existing
constraints are updated to treat ``wechat_native`` like ``zpay`` (PENDING→PAID
lifecycle, both pricing scopes, min/step ladder applies).  ``activation_code``
and ``admin_adjustment`` stay untouched — their removal is CW-084 / migration 093.

PostgreSQL only (026 precedent).  SQLite remains the internal P0 runtime whose
recharge_orders only ever holds zpay/INTERNAL rows.

Revision ID: 083_recharge_orders_multi_provider
Revises: 081_oral_unit_price
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "083_recharge_orders_multi_provider"
down_revision = "086_remove_device_slot_constraints"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Constraint definitions (post-migration target state)
# ---------------------------------------------------------------------------

_PROVIDER_ENUM = "provider IN ('zpay', 'activation_code', 'admin_adjustment', 'wechat_native')"

_PROVIDER_SCOPE_PAIRING = (
    "(provider = 'activation_code' AND pricing_scope = 'CUSTOMER_STANDARD') OR "
    "(provider IN ('zpay', 'admin_adjustment', 'wechat_native') "
    "AND pricing_scope IN ('INTERNAL', 'CUSTOMER_STANDARD'))"
)

# wechat_native follows the same PENDING→PAID lifecycle as zpay.
_PROVIDER_STATUS = (
    "provider IN ('zpay', 'wechat_native') OR "
    "(provider IN ('activation_code', 'admin_adjustment') AND status = 'PAID')"
)

# wechat_native stores its trade reference in ``transaction_id``, not
# ``provider_trade_no``; the latter stays NULL for wechat_native rows.
_PROVIDER_TRADE_NO = (
    "(provider = 'zpay' AND status = 'PAID' AND provider_trade_no IS NOT NULL) OR "
    "(provider = 'zpay' AND status != 'PAID' AND provider_trade_no IS NULL) OR "
    "(provider IN ('activation_code', 'admin_adjustment') AND provider_trade_no IS NULL) OR "
    "(provider = 'wechat_native' AND provider_trade_no IS NULL)"
)

# 036's channel guard: channel is only meaningful for payment-gateway providers.
_PROVIDER_CHANNEL = "channel IS NULL OR provider IN ('zpay', 'wechat_native')"

# Min/step ladder governs both gateway providers.
_AMOUNT_MINIMUM = (
    "provider NOT IN ('zpay', 'wechat_native') OR amount_fen >= min_recharge_fen_snapshot"
)
_AMOUNT_STEP = (
    "provider NOT IN ('zpay', 'wechat_native') OR amount_fen % recharge_step_fen_snapshot = 0"
)

# WeChat-specific column guards -------------------------------------------

# prepay_id and code_url are set at order creation (PENDING) for wechat_native only.
_WECHAT_PREPAY_ID = (
    "(provider = 'wechat_native' AND prepay_id IS NOT NULL) OR "
    "(provider != 'wechat_native' AND prepay_id IS NULL)"
)
_WECHAT_CODE_URL = (
    "(provider = 'wechat_native' AND code_url IS NOT NULL) OR "
    "(provider != 'wechat_native' AND code_url IS NULL)"
)
# transaction_id is set when the payment is confirmed (PAID) for wechat_native.
_WECHAT_TRANSACTION_ID = (
    "(provider = 'wechat_native' AND status = 'PAID' AND transaction_id IS NOT NULL) OR "
    "(provider = 'wechat_native' AND status != 'PAID' AND transaction_id IS NULL) OR "
    "(provider != 'wechat_native' AND transaction_id IS NULL)"
)

# Constraints replaced by this revision.
_REPLACED_CONSTRAINTS = (
    "ck_recharge_orders_provider",
    "ck_recharge_orders_provider_scope",
    "ck_recharge_orders_provider_status",
    "ck_recharge_orders_provider_trade_no",
    "ck_recharge_orders_provider_channel",
    "ck_recharge_orders_amount_minimum",
    "ck_recharge_orders_amount_step",
)

_NEW_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("ck_recharge_orders_provider", _PROVIDER_ENUM),
    ("ck_recharge_orders_provider_scope", _PROVIDER_SCOPE_PAIRING),
    ("ck_recharge_orders_provider_status", _PROVIDER_STATUS),
    ("ck_recharge_orders_provider_trade_no", _PROVIDER_TRADE_NO),
    ("ck_recharge_orders_provider_channel", _PROVIDER_CHANNEL),
    ("ck_recharge_orders_amount_minimum", _AMOUNT_MINIMUM),
    ("ck_recharge_orders_amount_step", _AMOUNT_STEP),
    ("ck_recharge_orders_wechat_prepay_id", _WECHAT_PREPAY_ID),
    ("ck_recharge_orders_wechat_code_url", _WECHAT_CODE_URL),
    ("ck_recharge_orders_wechat_transaction_id", _WECHAT_TRANSACTION_ID),
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 1. Add WeChat-specific columns (nullable, no default).
    op.add_column("recharge_orders", sa.Column("prepay_id", sa.Text(), nullable=True))
    op.add_column("recharge_orders", sa.Column("code_url", sa.Text(), nullable=True))
    op.add_column("recharge_orders", sa.Column("transaction_id", sa.Text(), nullable=True))

    # 2. Replace provider-related constraints.
    for name in _REPLACED_CONSTRAINTS:
        op.drop_constraint(name, "recharge_orders", type_="check")
    for name, condition in _NEW_CONSTRAINTS:
        op.create_check_constraint(name, "recharge_orders", condition)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Guard: refuse if wechat_native rows exist (billing rows must never be
    # deleted to make a downgrade pass — 026 No-Go precedent).
    has_wechat_rows = bind.execute(
        sa.text("SELECT EXISTS (  SELECT 1 FROM recharge_orders WHERE provider = 'wechat_native')")
    ).scalar()
    if has_wechat_rows:
        raise RuntimeError(
            "cannot downgrade 083_recharge_orders_multi_provider: "
            "recharge_orders already holds wechat_native rows, which the "
            "pre-083 constraints cannot hold. Keep revision 083, or resolve "
            "the ledger manually before rolling back."
        )

    # 1. Restore pre-083 constraints.
    for name, _condition in _NEW_CONSTRAINTS:
        op.drop_constraint(name, "recharge_orders", type_="check")

    # Restore the 026/036 shapes verbatim.
    op.create_check_constraint(
        "ck_recharge_orders_provider",
        "recharge_orders",
        "provider IN ('zpay', 'activation_code', 'admin_adjustment')",
    )
    op.create_check_constraint(
        "ck_recharge_orders_provider_scope",
        "recharge_orders",
        "(provider = 'activation_code' AND pricing_scope = 'CUSTOMER_STANDARD') OR "
        "(provider IN ('zpay', 'admin_adjustment') "
        "AND pricing_scope IN ('INTERNAL', 'CUSTOMER_STANDARD'))",
    )
    op.create_check_constraint(
        "ck_recharge_orders_provider_status",
        "recharge_orders",
        "provider = 'zpay' OR "
        "(provider IN ('activation_code', 'admin_adjustment') AND status = 'PAID')",
    )
    op.create_check_constraint(
        "ck_recharge_orders_provider_trade_no",
        "recharge_orders",
        "(provider = 'zpay' AND status = 'PAID' AND provider_trade_no IS NOT NULL) OR "
        "(provider = 'zpay' AND status != 'PAID' AND provider_trade_no IS NULL) OR "
        "(provider IN ('activation_code', 'admin_adjustment') AND provider_trade_no IS NULL)",
    )
    op.create_check_constraint(
        "ck_recharge_orders_provider_channel",
        "recharge_orders",
        "channel IS NULL OR provider = 'zpay'",
    )
    op.create_check_constraint(
        "ck_recharge_orders_amount_minimum",
        "recharge_orders",
        "provider != 'zpay' OR amount_fen >= min_recharge_fen_snapshot",
    )
    op.create_check_constraint(
        "ck_recharge_orders_amount_step",
        "recharge_orders",
        "provider != 'zpay' OR amount_fen % recharge_step_fen_snapshot = 0",
    )

    # 2. Drop WeChat columns.
    op.drop_column("recharge_orders", "transaction_id")
    op.drop_column("recharge_orders", "code_url")
    op.drop_column("recharge_orders", "prepay_id")
