"""Account credit tariffs. Reservations and orders keep acceptance-time prices."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.db_portable import BusinessConnection

Subject = Literal["video_768p", "video_2k", "oral"]
MAX_CREDITS = 2_147_483_647


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    video_768p: int | None = Field(default=None, ge=0, le=1_000_000)
    video_2k: int | None = Field(default=None, ge=0, le=1_000_000)
    oral: int | None = Field(default=None, ge=0, le=1_000_000)
    points_per_yuan: int = Field(ge=1, le=1_000_000)
    discount_basis_points: int = Field(default=10_000, ge=1, le=10_000)
    consumption_rounding: Literal["ceil", "floor"] = "ceil"


def task_credits(config: PricingConfig, subject: Subject, units: int, quantity: int = 1) -> int:
    if units < 1 or quantity < 1:
        raise ValueError("计费数量必须大于零")
    unit_price = getattr(config, subject)
    if not unit_price:
        return 0
    numerator = int(unit_price) * units * config.discount_basis_points
    per_task = max(1, (numerator + (9999 if config.consumption_rounding == "ceil" else 0)) // 10000)
    total = per_task * quantity
    if total > MAX_CREDITS:
        raise ValueError("积分金额超出允许范围")
    return total


def recharge_credits(config: PricingConfig, amount_fen: int) -> int:
    credits = amount_fen * config.points_per_yuan // 100
    if amount_fen < 1 or not 1 <= credits <= MAX_CREDITS:
        raise ValueError("充值金额对应的积分超出允许范围")
    return credits


def read_pricing(conn: BusinessConnection) -> tuple[int, PricingConfig | None]:
    # Offline historical input remains on the exact legacy schema.
    if not conn.is_postgres:
        return 0, None
    row = conn.execute(
        "SELECT version, config_json FROM customer_credit_pricing WHERE id = 1 FOR SHARE"
    ).fetchone()
    if row is None:
        raise RuntimeError("积分计价配置行缺失")
    return int(row["version"]), (
        PricingConfig.model_validate_json(str(row["config_json"])) if row["config_json"] else None
    )


def quote_snapshot(conn: BusinessConnection, subject: Subject, units: int) -> tuple[int, str]:
    from app.billing_catalog import retail_snapshot

    snapshot = retail_snapshot(conn, subject, units)
    return int(str(snapshot["credits"])), json.dumps(snapshot, separators=(",", ":"))
