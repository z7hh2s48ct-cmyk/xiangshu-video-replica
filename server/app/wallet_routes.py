from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from app.auth import AuthenticatedUser, Database
from app.settings import DEFAULT_BILLING_SETTINGS

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class WalletResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_credits: int
    reserved_credits: int
    internal_unit_price_fen: int | None = None
    min_recharge_fen: int | None = None
    recharge_step_fen: int | None = None
    points_per_yuan: int | None = None
    credit_price_version: int | None = None


class WalletTransactionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    user_id: str
    type: Literal["CHARGE", "RESERVE", "SETTLE", "RELEASE", "CONVERSION"]
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


class WalletTransactionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[WalletTransactionResponse]
    total: int
    limit: int
    offset: int


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
