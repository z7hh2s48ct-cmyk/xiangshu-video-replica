"""Explicit once-only legacy wallet conversion, with versioned policy and audit."""

import json
from typing import Literal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract, write_with_idempotency
from app.db_pg import pg_transaction
from app.wechat_native_client import NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS

router = APIRouter(prefix="/api/control", tags=["legacy-credit-conversion"])


class LegacyPolicy(BaseModel):
    version: int
    mode: Literal["keep", "convert"]
    numerator: int
    denominator: int


class PolicyUpdate(AdminWriteContract):
    expected_version: int = Field(ge=0, strict=True)
    mode: Literal["keep", "convert"]
    numerator: int = Field(ge=1, le=1000000, strict=True)
    denominator: int = Field(ge=1, le=1000000, strict=True)


class ConversionRequest(AdminWriteContract):
    expected_version: int = Field(ge=0, strict=True)
    expected_balance: int = Field(ge=0, le=2147483647, strict=True)


class ConversionPreview(BaseModel):
    user_id: str
    before_credits: int
    after_credits: int
    policy: LegacyPolicy
    converted: bool


def read_policy(conn: psycopg.Connection) -> LegacyPolicy:
    row = conn.execute(
        "SELECT version, mode, numerator, denominator FROM "
        "legacy_credit_policy WHERE id = 1 FOR SHARE"
    ).fetchone()
    if row is None:
        raise HTTPException(503, detail="历史积分策略暂不可用。")
    return LegacyPolicy(version=row[0], mode=row[1], numerator=row[2], denominator=row[3])


@router.get("/settings/legacy-credit-policy", response_model=LegacyPolicy)
def get_policy(response: Response, _actor: AdminReader) -> LegacyPolicy:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        return read_policy(conn)


@router.put("/settings/legacy-credit-policy", response_model=LegacyPolicy)
def save_policy(
    body: PolicyUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT version FROM legacy_credit_policy WHERE id = 1 FOR UPDATE"
        ).fetchone()
        if row is None or row[0] != body.expected_version:
            raise HTTPException(409, detail="历史积分策略已变化，请刷新后重试。")
        conn.execute(
            (
                "UPDATE legacy_credit_policy SET version = version + 1, mode "
                "= %s, numerator = %s, denominator = %s WHERE id = 1"
            ),
            (body.mode, body.numerator, body.denominator),
        )
        conn.execute(
            (
                "INSERT INTO audit_logs (id, actor_user_id, action, "
                "entity_type, entity_id, metadata_json) VALUES (%s, %s, "
                "'legacy_credit_policy.update', 'legacy_credit_policy', '1', "
                "%s)"
            ),
            (
                str(uuid4()),
                actor.user_id,
                json.dumps(
                    {"reason": body.reason, "request_id": request_id, "policy": body.model_dump()}
                ),
            ),
        )
        return read_policy(conn).model_dump()

    return write_with_idempotency(request, response, actor, body, business, success_status=200)


def preview_conversion(
    conn: psycopg.Connection, user_id: str, *, applying: bool = False
) -> ConversionPreview:
    policy = read_policy(conn)
    owner = conn.execute(
        (
            "SELECT id FROM users WHERE id = %s AND role = 'customer' "
            "AND (registration_source = 'activation_code' OR "
            "EXISTS(SELECT 1 FROM activation_code_activations a WHERE "
            "a.user_id = users.id)) FOR SHARE"
        ),
        (user_id,),
    ).fetchone()
    if owner is None:
        raise HTTPException(409, detail="仅旧激活账号可执行历史积分转换。")
    wallet = conn.execute(
        "SELECT available_credits, reserved_credits FROM wallets WHERE user_id = %s FOR UPDATE",
        (user_id,),
    ).fetchone()
    if wallet is None:
        raise HTTPException(404, detail="账号钱包不存在。")
    previous = conn.execute(
        (
            "SELECT before_credits, after_credits, policy_version, mode, "
            "numerator, denominator FROM wallet_credit_conversions WHERE "
            "user_id = %s"
        ),
        (user_id,),
    ).fetchone()
    if previous is not None:
        if applying:
            raise HTTPException(409, detail="该账号已完成转换，不可重复执行。")
        return ConversionPreview(
            user_id=user_id,
            before_credits=previous[0],
            after_credits=previous[1],
            policy=LegacyPolicy(
                version=previous[2],
                mode=previous[3],
                numerator=previous[4],
                denominator=previous[5],
            ),
            converted=True,
        )
    if wallet[1] != 0:
        raise HTTPException(409, detail="账号仍有待结算积分，暂不能转换。")
    mixed = conn.execute(
        (
            "SELECT EXISTS(SELECT 1 FROM wallet_transactions WHERE "
            "user_id = %s AND (auth_source IS NOT NULL OR "
            "pricing_snapshot_json IS NOT NULL)) OR EXISTS(SELECT 1 FROM "
            "recharge_orders WHERE user_id = %s AND "
            "(credit_pricing_snapshot_json IS NOT NULL OR (provider = 'zpay' "
            "AND status IN ('PENDING', 'CLOSED')) OR (provider = "
            "'wechat_native' AND status IN ('PENDING', 'CLOSED') AND "
            "created_at::timestamptz + make_interval(secs => %s) > now())))"
        ),
        # A wechat_native order is a top-up that may still complete only inside
        # its payment window plus the callback retry horizon; past that it can
        # no longer retroactively mix this wallet (same criterion as the
        # merchant-identity guard).
        (user_id, user_id, float(NATIVE_MERCHANT_SWITCH_BLOCK_SECONDS)),
    ).fetchone()
    if mixed and mixed[0]:
        raise HTTPException(409, detail="该账号已有新积分交易或可补付订单，需先核对账务。")
    before = int(wallet[0])
    after = before if policy.mode == "keep" else before * policy.numerator // policy.denominator
    if after > 2147483647:
        raise HTTPException(422, detail="转换后积分超过钱包上限。")
    return ConversionPreview(
        user_id=user_id, before_credits=before, after_credits=after, policy=policy, converted=False
    )


@router.get("/customers/{user_id}/credit-conversion", response_model=ConversionPreview)
def get_preview(user_id: str, response: Response, _actor: AdminReader) -> ConversionPreview:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        return preview_conversion(conn, user_id)


@router.post("/customers/{user_id}/credit-conversion", response_model=ConversionPreview)
def apply_conversion(
    user_id: str, body: ConversionRequest, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        preview = preview_conversion(conn, user_id, applying=True)
        if (
            preview.policy.version != body.expected_version
            or preview.before_credits != body.expected_balance
        ):
            raise HTTPException(409, detail="策略或钱包余额已变化，请重新预览。")
        difference = preview.after_credits - preview.before_credits
        ledger_id = str(uuid4()) if difference else None
        if ledger_id:
            conn.execute(
                (
                    "INSERT INTO wallet_transactions (id, user_id, type, "
                    "available_delta, reserved_delta, idempotency_key, "
                    "auth_source) VALUES (%s, %s, 'CONVERSION', %s, 0, %s, "
                    "'internal')"
                ),
                (ledger_id, user_id, difference, "credit-conversion:" + user_id),
            )
            conn.execute(
                (
                    "UPDATE wallets SET available_credits = %s, updated_at = "
                    "CURRENT_TIMESTAMP WHERE user_id = %s"
                ),
                (preview.after_credits, user_id),
            )
        conn.execute(
            (
                "INSERT INTO wallet_credit_conversions (user_id, "
                "before_credits, after_credits, policy_version, mode, "
                "numerator, denominator, ledger_id, actor_user_id, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ),
            (
                user_id,
                preview.before_credits,
                preview.after_credits,
                preview.policy.version,
                preview.policy.mode,
                preview.policy.numerator,
                preview.policy.denominator,
                ledger_id,
                actor.user_id,
                body.reason,
            ),
        )
        conn.execute(
            (
                "INSERT INTO audit_logs (id, actor_user_id, action, "
                "entity_type, entity_id, metadata_json) VALUES (%s, %s, "
                "'legacy_credit.converted', 'user', %s, %s)"
            ),
            (
                str(uuid4()),
                actor.user_id,
                user_id,
                json.dumps(
                    {
                        "reason": body.reason,
                        "request_id": request_id,
                        "before": preview.before_credits,
                        "after": preview.after_credits,
                    }
                ),
            ),
        )
        return preview.model_copy(update={"converted": True}).model_dump()

    return write_with_idempotency(request, response, actor, body, business, success_status=200)
