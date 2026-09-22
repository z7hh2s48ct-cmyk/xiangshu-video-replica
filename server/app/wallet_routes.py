from __future__ import annotations

from decimal import Decimal
from typing import Literal, cast

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database
from app.settings import DEFAULT_BILLING_SETTINGS

router = APIRouter(prefix="/api/wallet", tags=["wallet"])

UNIT_ROUNDING = Literal["ceil", "exact"]
CONSUMPTION_ROUNDING = Literal["ceil", "floor"]


class WalletResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_credits: int
    reserved_credits: int
    internal_unit_price_fen: int | None = None
    min_recharge_fen: int | None = None
    recharge_step_fen: int | None = None
    points_per_yuan: int | None = None
    credit_price_version: int | None = None


class WalletTransactionPricing(BaseModel):
    """One ledger row's pricing basis, projected for the customer lane (P0-3).

    The frozen snapshot answers *how* a charge was priced — unit price, usage,
    discount, rounding — without which a customer sees only the delta. Only its
    retail side crosses this boundary: the cost side (``unit_cost_fen``,
    funding lots, revenue) never does, so a snapshot that happens to carry such
    keys is filtered field by field instead of being dumped.
    """

    model_config = ConfigDict(extra="forbid")

    service: str | None = None
    version: int | None = None
    unit: str | None = None
    units: str | None = None
    unit_credits: str | None = None
    unit_rounding: UNIT_ROUNDING | None = None
    discount_basis_points: int | None = None
    consumption_rounding: CONSUMPTION_ROUNDING | None = None
    # The submitted budget's credits: the charge ceiling this row was frozen
    # with, while the deltas on the same row are what actually moved.
    credits: int | None = None
    enabled: bool | None = None
    free_reason: str | None = None


class WalletTransactionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    type: Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE", "CONVERSION", "REFUND"]
    available_delta: int
    reserved_delta: int
    recharge_order_id: str | None
    task_id: str | None
    billing_round: int | None
    created_at: str
    oral_task_id: str | None = None
    api_key_id: str | None = None
    token_group_id: str | None = None
    token_label: str | None = None
    credential_version: int | None = None
    auth_source: str | None = None
    credit_price_version: int | None = None
    generation_batch_id: str | None = None
    credit_source: str | None = None
    billing_operation_id: str | None = None
    service: str | None = None
    service_name: str | None = None
    # T2.10: who caused the entry — a sub-account id when the consumption rode
    # the master's wallet; NULL on historical rows without actor evidence.
    actor_user_id: str | None = None
    actor_name: str | None = None
    # P0-3: why this row moved — the retail side of the frozen snapshot.
    pricing: WalletTransactionPricing | None = None
    # P1-7: 同一计费周期的配对态（RESERVE→SETTLE/RELEASE 共享一个
    # billing_operation_id）。组内每行取同一答案：有结算即 SETTLED、全额
    # 退回为 RELEASED、仅预扣为 PENDING；充值/历史行无组为 None。
    pair_state: Literal["PENDING", "SETTLED", "RELEASED"] | None = None


class WalletTransactionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[WalletTransactionResponse]
    total: int
    limit: int
    offset: int
    # B3: optional aggregation summary array
    sub_account_summary: list[dict[str, str | int]] | None = None


class ConsumptionByBusinessItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    business: str
    credits: int


class ConsumptionByBusinessResponse(BaseModel):
    """近 N 天的消费构成（审计方案 F / P1 清单 #10）。

    口径：只算 ``SETTLE``（真正结算掉的消费），不含退回与充值；窗口是**滚动**
    的 N×24 小时，不做自然日对齐——「最近 30 天」按滚动窗口解释更直白，也免得
    在时区边界上多一层解释。
    """

    model_config = ConfigDict(extra="forbid")

    days: int
    total_credits: int
    items: list[ConsumptionByBusinessItem]


def _snapshot_text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _snapshot_number_text(value: object) -> str | None:
    """Snapshot amounts are frozen as text (``str(tariff.unit_credits)``)."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _snapshot_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _snapshot_choice(value: object, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def pricing_breakdown(snapshot: object) -> WalletTransactionPricing | None:
    """Whitelist projection of a frozen pricing snapshot (BILLING-OBS-20260922 P0-3).

    ``snapshot`` is stored JSON: an unusable shape and any field that is not a
    recognised retail value degrade to ``None`` individually, so one corrupt
    historical row can never turn the whole ledger page into a 500.
    """
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    return WalletTransactionPricing(
        service=_snapshot_text(snapshot.get("service")),
        version=_snapshot_int(snapshot.get("version")),
        unit=_snapshot_text(snapshot.get("unit")),
        units=_snapshot_number_text(snapshot.get("units")),
        unit_credits=_snapshot_number_text(snapshot.get("unit_credits")),
        unit_rounding=cast(
            UNIT_ROUNDING | None, _snapshot_choice(snapshot.get("unit_rounding"), ("ceil", "exact"))
        ),
        discount_basis_points=_snapshot_int(snapshot.get("discount_basis_points")),
        consumption_rounding=cast(
            CONSUMPTION_ROUNDING | None,
            _snapshot_choice(snapshot.get("consumption_rounding"), ("ceil", "floor")),
        ),
        credits=_snapshot_int(snapshot.get("credits")),
        enabled=snapshot.get("enabled") if isinstance(snapshot.get("enabled"), bool) else None,
        free_reason=_snapshot_text(snapshot.get("free_reason")),
    )


@router.get("", response_model=WalletResponse, response_model_exclude_none=True)
def read_wallet(conn: Database, actor: AuthenticatedUser) -> WalletResponse:
    row = conn.execute(
        """
        SELECT available_credits, reserved_credits
        FROM wallets
        WHERE user_id = %s
        """,
        (actor.id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "WALLET_NOT_FOUND", "message": "Wallet does not exist."},
        )
    billing_row = conn.execute(
        """
        SELECT internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen
        FROM runtime_settings
        WHERE id = 1
        """
    ).fetchone()
    billing = (
        DEFAULT_BILLING_SETTINGS
        if billing_row is None
        else {
            "internal_base_unit_price_fen": int(billing_row["internal_base_unit_price_fen"]),
            "min_recharge_fen": int(billing_row["min_recharge_fen"]),
            "recharge_step_fen": int(billing_row["recharge_step_fen"]),
        }
    )
    expose_internal_prices = actor.role != "customer"
    return WalletResponse(
        available_credits=int(row["available_credits"]),
        reserved_credits=int(row["reserved_credits"]),
        internal_unit_price_fen=(
            billing["internal_base_unit_price_fen"] if expose_internal_prices else None
        ),
        min_recharge_fen=billing["min_recharge_fen"] if expose_internal_prices else None,
        recharge_step_fen=billing["recharge_step_fen"] if expose_internal_prices else None,
    )


@router.get("/transactions", response_model=WalletTransactionPage)
def list_wallet_transactions(
    conn: Database,
    actor: AuthenticatedUser,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> WalletTransactionPage:
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE user_id = %s",
            (actor.id,),
        ).fetchone()[0]
    )
    rows = conn.execute(
        """
        SELECT
            id, user_id, type, available_delta, reserved_delta,
            recharge_order_id, task_id, oral_task_id, billing_round, created_at
        FROM wallet_transactions
        WHERE user_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s OFFSET %s
        """,
        (actor.id, limit, offset),
    ).fetchall()
    return WalletTransactionPage(
        items=[WalletTransactionResponse(**dict(row)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
