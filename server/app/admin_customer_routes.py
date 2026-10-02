"""T23 / BILL-02 — audited admin adjustments API.

Application layer on top of the T26/T27 billing schema (recharge_orders, wallets,
wallet_transactions). Every adjustment lands as one atomic transaction.

**Forward** (``credits >= 1``) writes:

1. A `provider='admin_adjustment'` `status='PAID'` recharge order (revision 026 shapes;
   no third-party trade number, created PAID by double confirmation)
2. A wallet `CHARGE` ledger row with `available_delta = credits`, `task_id=NULL`
3. The atomic wallet credit increment (available_credits += credits)
4. One append-only `admin_adjustments` audit row naming the real administrator (§15)

**Reverse** (``credits < 0``, only for ``REVERSAL_SOURCE_DOCUMENT_TYPES`` — B1,
SOP §10 step 2 "反向调账") writes:

1. A wallet `REFUND` ledger row with `available_delta = credits < 0`, attached to
   no order / task / billing round (the 20260923T1200 shape branch)
2. The atomic wallet decrement (available_credits += credits), refused with
   400 ``ADJUSTMENT_BALANCE_INSUFFICIENT`` when it would go below zero
3. One append-only `admin_adjustments` audit row with ``recharge_order_id`` NULL

There is **no recharge order** on the reverse path (D4) and ``amount_fen`` is 0:
the money itself is refunded out of band in the ZPay back office, and the two
facts are aligned by ``source_document_ref``. See the D2/D4 note further down.

Write contract (dev doc §15): every write behind the full admin gate — real admin
session (auditors are read-only), Idempotency-Key header (400 without it),
confirm=true (400 CONFIRMATION_REQUIRED), non-blank reason (400 REASON_REQUIRED).
The same four-part contract covers the reverse path; there is no second approver
(D5).

Idempotency (revision 031 snapshot layer): each business write runs inside a
PostgreSQL transaction that inserts an `admin_write_idempotency` placeholder keyed by
(actor, route, key digest). The winner back-fills the response snapshot before commit;
same-key retry replays the stored response (X-Idempotent-Replay: true); same key against
different params answers 409 IDEMPOTENCY_CONFLICT. Business failure rolls the placeholder
back so the key stays reusable.

Amount calculation: amount_fen = credits * the customer's effective unit price frozen
on the order. The internal base price remains a separate reporting snapshot and does
not constrain the customer sale price.

Pricing scope inference: a target user bound to an activation code is CUSTOMER_STANDARD;
an internal account stays INTERNAL (revision 026 pairing).

Zero ledger difference invariant: after any adjustment the wallet balance moved by
exactly credits, and the matching ledger row (CHARGE forward, REFUND reverse) is in
the same transaction — no balance mutation without its ledger row (禁止直接改余额).

Fail-closed runtime: SQLite/missing DSN returns 503 ADJUSTMENT_SERVICE_UNAVAILABLE instead
of falling back to legacy control identity (the T12/T18 precedent).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Literal, Never, cast

import psycopg
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import Response as HttpResponse
from pydantic import BaseModel, ConfigDict, StrictInt

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_cash_evidence import CASH_ORDER_ELIGIBLE
from app.admin_customer_metrics import (
    CUSTOMER_FACTS_CTE,
    CUSTOMER_METRICS_JOINS,
    INACTIVE_SQL,
    CustomerAttention,
    append_attention_filter,
    utc_text_timestamp,
)
from app.admin_dates import append_admin_date_filters
from app.admin_write_contract import (
    AdminWriteActor,
    DeferredHTTPWriteError,
)
from app.admin_write_contract import (
    AdminWriteContract as AdminWriteRequest,
)
from app.admin_write_contract import (
    http_error as _http,
)
from app.admin_write_contract import (
    transaction_now_iso as _transaction_now_iso,
)
from app.admin_write_contract import (
    write_with_idempotency as _shared_write_with_idempotency,
)
from app.auth import CurrentUser, Role
from app.csv_export import spreadsheet_safe_cell
from app.customer_session_service import revoke_session
from app.db_pg import MissingDatabaseConfigError, pg_transaction
from app.db_portable import BusinessConnection
from app.permissions import write_audit
from app.security_rate_limit import (
    DIMENSION_CONTROL_EXPORT_ACCOUNT,
    consume_rate_limit,
    control_export_account_limit,
    rate_limit_window_seconds,
)
from app.settings import apply_customer_unit_price
from app.sql_pagination import PAGE_CLAUSE, page_bounds

router = APIRouter(prefix="/api/control", tags=["admin-customers"])

# 本模块自己的 logger。2026-09-12 评审 P3 记载的「跨路由借 logger」曾在这里
# 函数体内 `from app.admin_activation_routes import logger` —— 借来的 logger
# 打的是别模块的名字，日志来源会被误标，也平白多一条模块依赖。
logger = logging.getLogger(__name__)


def _sqlite_lane() -> bool:
    """True on the internal SQLite lane (no VIDEO_REPLICA_DATABASE_URL)."""
    import os

    from app.db_pg import DATABASE_URL_ENV

    return not bool(os.environ.get(DATABASE_URL_ENV, "").strip())


def _write_with_idempotency(
    request: Request,
    response: Response,
    actor: AdminWriteActor,
    body: AdminWriteRequest,
    business: Callable[[psycopg.Connection, str], dict[str, object]],
    *,
    success_status: int = 201,
    unavailable_code: str = "ADJUSTMENT_SERVICE_UNAVAILABLE",
    unavailable_message: str = "Admin adjustments require the PostgreSQL runtime.",
) -> dict[str, object]:
    """The shared envelope bound to the customer lane's fail-closed defaults."""
    return _shared_write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=success_status,
        unavailable_code=unavailable_code,
        unavailable_message=unavailable_message,
    )


# The frozen source-document enum (来源单类型, revision 039 CHECK constraint).
SOURCE_DOCUMENT_TYPES = (
    "CS_TICKET",
    "REFUND_APPROVAL",
    "COMPENSATION_APPROVAL",
    "LEDGER_CORRECTION",
    # 运营发放免费生成条数（054）：不产生支付金额，amount_fen 记 0。
    "FREE_GRANT",
    "CREDIT_COMPENSATION",
)

# 允许**负向**（反向调账）的来源单类型。SOP §10 第 2 步要求退款走「管理员审计调账
# 反向调账 + ZPay 后台人工退付」双事实对齐；在此之前枚举里虽有「退款审批」，通道
# 却只收正数，动作与标签方向相反。这里只放开「可以填负数」这一件事：账本侧冲减
# credits（`REFUND` 类型），资金侧仍在 ZPay 后台人工办理、以来源单号对齐留档。
# 其余来源类型维持正向口径（1 <= credits），文案与校验零变化。
REVERSAL_SOURCE_DOCUMENT_TYPES = frozenset({"REFUND_APPROVAL", "LEDGER_CORRECTION"})

# --- 反向调账的两条设计边界（已拍板，不是待解决缺口）-----------------------
#
# D2（行业惯例：退款只覆盖未消耗部分）：钱包下限仍是 026 的
# `ck_wallets_available_nonnegative`，**不允许负余额**，也不做透支分支。因此反向
# 调账的上限就是客户**当前可用余额**；客户已消耗的额度对应已交付服务，账本层面
# 不予退回（争议走线下 / 拒付核销通道，不在本任务范围）。SOP §10 第 3 步的
# 「事后差额为零」因此只在未消耗部分成立——这是口径本身的边界，不是实现缺陷：
# 若将来业务要求连已消耗部分也能退，那是「允许透支」的另一条方案，需要单独批准，
# 不要在这里放宽。
#
# D4（退款不产生充值单）：反向调账**不建 `recharge_orders` 行**——`ck_recharge_orders_status`
# 没有退款态，且 `reconcile_customer_billing` 要求每张 PAID 单恰好对应一条同额
# `CHARGE`（正向形状），用负 credits 造单必然对不平。审计留痕落在 `admin_adjustments`
# 行上（`recharge_order_id` 由 20260923T1200 放开为可空，仅反向调账为 NULL）。


# ---------------------------------------------------------------------------
# Admin write contract (dev doc §15)
# ---------------------------------------------------------------------------


class AdjustmentRequest(AdminWriteRequest):
    """Shared request shape for every admin adjustment write."""

    credits: StrictInt = 0
    source_document_type: str = ""
    source_document_ref: str = ""


# ---------------------------------------------------------------------------
# Pricing scope inference
# ---------------------------------------------------------------------------


def _infer_pricing_scope(conn: psycopg.Connection, user_id: str) -> str:
    """Infer pricing scope from whether the target user has activation binding.

    A user bound to a *current* activation code is CUSTOMER_STANDARD — the
    same current-binding rule as revision 027's partial unique index
    (ACTIVE or SUSPENDED; a REVOKED code keeps its binding for audit only
    and does not count as a current binding). Otherwise INTERNAL.
    """
    row = conn.execute(
        """
        SELECT 1 FROM activation_code_activations acca
        JOIN activation_codes acaba ON acaba.id = acca.code_id
        WHERE acca.user_id = %s AND acaba.status IN ('ACTIVE', 'SUSPENDED')
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if row is not None:
        return "CUSTOMER_STANDARD"
    registered = conn.execute(
        "SELECT 1 FROM users WHERE id = %s AND role = 'customer' "
        "AND registration_source IN ('self_register', 'activation_code')",
        (user_id,),
    ).fetchone()
    return "CUSTOMER_STANDARD" if registered else "INTERNAL"


def _current_billing_snapshot(
    conn: psycopg.Connection, *, user_id: str, pricing_scope: str
) -> dict[str, int]:
    """The billing snapshot frozen onto an admin-written order (base/charged/min/step)."""
    snapshot = conn.execute(
        "SELECT internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen "
        "FROM runtime_settings WHERE id = 1"
    ).fetchone()
    if not snapshot:
        raise _http(503, "BILLING_SNAPSHOT_UNAVAILABLE", "Billing snapshot not configured.")

    base_unit_price_fen = int(snapshot[0])
    billing = {
        "internal_base_unit_price_fen": base_unit_price_fen,
        "charged_unit_price_fen": base_unit_price_fen,
        "min_recharge_fen": int(snapshot[1]),
        "recharge_step_fen": int(snapshot[2]),
    }
    if pricing_scope == "CUSTOMER_STANDARD":
        custom_price = conn.execute(
            "SELECT unit_price_fen FROM customer_unit_prices WHERE user_id = %s",
            (user_id,),
        ).fetchone()
        if custom_price is not None:
            billing = apply_customer_unit_price(
                billing,
                unit_price_fen=int(custom_price[0]),
            )
    return billing


def _deny_admin_self_service(
    conn: psycopg.Connection,
    *,
    actor_user_id: str,
    target_user_id: str,
    attempted_action: str,
    reason: str,
    request_id: str,
) -> Never:
    conn.execute(
        """
        INSERT INTO audit_logs
            (id, actor_user_id, action, entity_type, entity_id, metadata_json)
        VALUES (%s, %s, 'security.admin_self_service_denied', 'user', %s, %s)
        """,
        (
            str(uuid.uuid4()),
            actor_user_id,
            target_user_id,
            json.dumps(
                {
                    "attempted_action": attempted_action,
                    "reason": reason,
                    "request_id": request_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
    )
    raise DeferredHTTPWriteError(
        403,
        "ADMIN_SELF_SERVICE_FORBIDDEN",
        "An administrator cannot change their own balance or pricing.",
    )


class CustomerUnitPriceUpdateRequest(AdminWriteRequest):
    model_config = ConfigDict(extra="forbid")

    # ``null`` removes the override and returns the customer to the global
    # default. StrictInt prevents booleans and numeric strings from silently
    # becoming financial values.
    unit_price_fen: StrictInt | None


class CustomerUnitPriceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    unit_price_fen: int
    custom_unit_price_fen: int | None
    default_unit_price_fen: int
    min_recharge_fen: int
    recharge_step_fen: int
    updated_at: str | None
    request_id: str | None = None


def _customer_unit_price_payload(
    conn: psycopg.Connection,
    *,
    user_id: str,
    request_id: str | None = None,
) -> dict[str, object]:
    row = conn.execute(
        """
        SELECT u.id,
               rs.internal_base_unit_price_fen,
               rs.min_recharge_fen,
               rs.recharge_step_fen,
               cup.unit_price_fen,
               cup.updated_at
        FROM users u
        CROSS JOIN runtime_settings rs
        LEFT JOIN customer_unit_prices cup ON cup.user_id = u.id
        WHERE u.id = %s
          AND rs.id = 1
          AND EXISTS (
              SELECT 1
              FROM activation_code_activations aca
              JOIN activation_codes ac ON ac.id = aca.code_id
              WHERE aca.user_id = u.id
                AND ac.status IN ('ACTIVE', 'SUSPENDED')
          )
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        raise _http(404, "CUSTOMER_NOT_FOUND", "Activated customer not found.")

    default_unit_price_fen = int(row[1])
    custom_unit_price_fen = None if row[4] is None else int(row[4])
    billing = {
        "internal_base_unit_price_fen": default_unit_price_fen,
        "charged_unit_price_fen": default_unit_price_fen,
        "min_recharge_fen": int(row[2]),
        "recharge_step_fen": int(row[3]),
    }
    if custom_unit_price_fen is not None:
        billing = apply_customer_unit_price(
            billing,
            unit_price_fen=custom_unit_price_fen,
        )
    return {
        "user_id": str(row[0]),
        "unit_price_fen": billing["charged_unit_price_fen"],
        "custom_unit_price_fen": custom_unit_price_fen,
        "default_unit_price_fen": default_unit_price_fen,
        "min_recharge_fen": billing["min_recharge_fen"],
        "recharge_step_fen": billing["recharge_step_fen"],
        "updated_at": None if row[5] is None else str(row[5]),
        "request_id": request_id,
    }


@router.get(
    "/customers/{user_id}/unit-price",
    response_model=CustomerUnitPriceResponse,
)
def read_customer_unit_price(
    user_id: str,
    actor: AdminReader,
) -> dict[str, object]:
    del actor
    try:
        with pg_transaction() as conn:
            return _customer_unit_price_payload(conn, user_id=user_id)
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "CUSTOMER_PRICING_UNAVAILABLE",
            "Customer pricing requires the PostgreSQL runtime.",
        ) from exc


@router.put(
    "/customers/{user_id}/unit-price",
    response_model=CustomerUnitPriceResponse,
)
def update_customer_unit_price(
    user_id: str,
    body: CustomerUnitPriceUpdateRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Set or clear a customer's sale price without applying a cost floor."""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_unit_price.update",
                reason=body.reason.strip(),
                request_id=request_id,
            )
        # The user row exists even before an override does; serialize its audit snapshots.
        conn.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user_id,))
        current = _customer_unit_price_payload(conn, user_id=user_id)
        unit_price_fen = body.unit_price_fen
        if unit_price_fen is not None and not 1 <= unit_price_fen <= 2_147_483_647:
            raise _http(
                400,
                "CUSTOMER_PRICE_INVALID",
                "unit_price_fen must be between 1 and 2147483647.",
            )

        if unit_price_fen is None:
            conn.execute("DELETE FROM customer_unit_prices WHERE user_id = %s", (user_id,))
            action = "customer_unit_price.reset"
        else:
            conn.execute(
                """
                INSERT INTO customer_unit_prices
                    (user_id, unit_price_fen, updated_by_user_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE
                SET unit_price_fen = EXCLUDED.unit_price_fen,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = clock_timestamp()
                """,
                (user_id, unit_price_fen, actor.user_id),
            )
            action = "customer_unit_price.update"

        updated = _customer_unit_price_payload(conn, user_id=user_id, request_id=request_id)

        def price_state(value: dict[str, object]) -> dict[str, object]:
            custom = value["custom_unit_price_fen"]
            return {
                "mode": "DEFAULT" if custom is None else "CUSTOM",
                "custom_unit_price_fen": custom,
                "effective_unit_price_fen": value["unit_price_fen"],
            }

        conn.execute(
            """
            INSERT INTO audit_logs
                (id, actor_user_id, action, entity_type, entity_id, metadata_json)
            VALUES (%s, %s, %s, 'customer_unit_price', %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                action,
                user_id,
                json.dumps(
                    {
                        "old_unit_price_fen": current["custom_unit_price_fen"],
                        "new_unit_price_fen": unit_price_fen,
                        "price_change": {
                            "before": price_state(current),
                            "after": price_state(updated),
                        },
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        return updated

    return _write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code="CUSTOMER_PRICING_UNAVAILABLE",
        unavailable_message="Customer pricing requires the PostgreSQL runtime.",
    )


# ---------------------------------------------------------------------------
# Adjustment creation (happy path + validations)
# ---------------------------------------------------------------------------


@router.post("/customers/{user_id}/adjustments", status_code=201)
def create_admin_adjustment(
    user_id: str,
    body: AdjustmentRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """Create an admin adjustment.

    Forward (``credits >= 1``): PAID order + CHARGE + wallet + audit row.

    Reverse (``credits < 0``, only for ``REVERSAL_SOURCE_DOCUMENT_TYPES``):
    wallet decrement + ``REFUND`` ledger row + audit row, **no order row**
    (see the module-level D2/D4 notes). The real money refund happens in the
    ZPay back office and is aligned to this row by ``source_document_ref``.
    """

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer_adjustment.create",
                reason=body.reason.strip(),
                request_id=request_id,
            )

        # 来源单类型决定这笔调账能不能填负数，所以先取出来再校验金额。
        # 未在 `REVERSAL_SOURCE_DOCUMENT_TYPES` 里的类型（含拼错的类型名）仍走
        # 原来的正向口径，错误码与文案与既有行为逐字一致。
        source_document_type = body.source_document_type.strip()
        source_document_ref = body.source_document_ref.strip()
        if source_document_type in REVERSAL_SOURCE_DOCUMENT_TYPES:
            if body.credits == 0 or not -2147483647 <= body.credits <= 2147483647:
                raise _http(
                    400,
                    "ADJUSTMENT_VALIDATION_FAILED",
                    "Reversal credits must be a non-zero integer between "
                    "-2147483647 and 2147483647.",
                )
        elif not 1 <= body.credits <= 2147483647:
            raise _http(
                400, "ADJUSTMENT_VALIDATION_FAILED", "Credits must be between 1 and 2147483647."
            )

        # Validate the source document (来源单): frozen enum + non-blank ref —
        # the revision 039 CHECK constraints are the defense in depth, the
        # route answers the operator with a 400 before touching the ledger.
        if source_document_type not in SOURCE_DOCUMENT_TYPES:
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "source_document_type must be one of: " + ", ".join(SOURCE_DOCUMENT_TYPES),
            )
        if not source_document_ref:
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "source_document_ref must not be blank.",
            )

        # Check target user exists
        user_row = conn.execute("SELECT 1 FROM users WHERE id = %s", (user_id,)).fetchone()
        if not user_row:
            raise _http(404, "USER_NOT_FOUND", "Target user not found.")

        # Check wallet exists
        wallet_exists = conn.execute(
            "SELECT 1 FROM wallets WHERE user_id = %s", (user_id,)
        ).fetchone()
        if not wallet_exists:
            raise _http(404, "WALLET_NOT_FOUND", "Wallet not found for target user.")

        # Infer pricing scope from target user's activation status
        pricing_scope = _infer_pricing_scope(conn, user_id)

        billing = _current_billing_snapshot(conn, user_id=user_id, pricing_scope=pricing_scope)
        base_unit_price_fen = billing["internal_base_unit_price_fen"]
        unit_price_fen = billing["charged_unit_price_fen"]
        min_recharge_fen = billing["min_recharge_fen"]
        recharge_step_fen = billing["recharge_step_fen"]

        credits = body.credits
        # 反向调账：只有负向才走 REFUND 分支；REFUND_APPROVAL / LEDGER_CORRECTION
        # 填正数仍走既有的正向 CHARGE 通道（例如撤回一笔退款的补记）。
        is_reversal = credits < 0
        amount_fen = (
            0
            if is_reversal or source_document_type in {"FREE_GRANT", "CREDIT_COMPENSATION"}
            else credits * unit_price_fen
        )
        # Paid adjustments must fit the money column; no-money credits have no price product.
        if amount_fen > 2147483647:
            raise _http(
                400,
                "ADJUSTMENT_VALIDATION_FAILED",
                "The credits calculation would overflow the ledger integer range.",
            )

        # FREE_GRANT records no payment; the price snapshot remains auditable.
        # 反向调账同理记 0：系统内没有资金流水（不建充值单），实际退付在 ZPay
        # 后台人工办理，两笔事实以来源单号对齐——金额列不编造一个系统内不存在的数字。
        if is_reversal or source_document_type in {"FREE_GRANT", "CREDIT_COMPENSATION"}:
            amount_fen = 0

        # Note: the min/step recharge ladder only governs zpay orders
        # (revision 026 constraints); audited adjustment amounts are defined
        # by their source documents, so no min/step enforcement here.

        # Generate identifiers
        adjustment_id = str(uuid.uuid4())

        # Timestamp from PostgreSQL transaction clock (SES-01)
        paid_at = _transaction_now_iso(conn)
        now = paid_at

        order_id: str | None = None
        if is_reversal:
            # 反向调账的账本事实：一条 REFUND 流水，不挂订单 / 任务 / 口播任务 /
            # 计费操作 / 计费轮次（20260923T1200 新增的形状分支）。审计行
            # （admin_adjustments.recharge_order_id 为 NULL）与它同事务落库。
            refund_id = f"admin_adjustment:refund:{adjustment_id}"
            conn.execute(
                """
                INSERT INTO wallet_transactions
                (id, user_id, type, available_delta, reserved_delta, recharge_order_id,
                 task_id, oral_task_id, billing_round, idempotency_key, auth_source)
                VALUES (%s, %s, 'REFUND', %s, 0, NULL, NULL, NULL, NULL, %s, 'internal')
                """,
                (refund_id, user_id, credits, refund_id),
            )
        else:
            order_id = str(uuid.uuid4())

            # Insert recharge order (PAID, no third-party trade for admin_adjustment)
            conn.execute(
                """
                INSERT INTO recharge_orders
                (id, user_id, merchant_order_no, provider, status, pricing_scope,
                 base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
                 min_recharge_fen_snapshot, recharge_step_fen_snapshot,
                 amount_fen, credits, paid_at)
                VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', %s,
                        %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    order_id,
                    user_id,
                    f"ADJ-{adjustment_id}",  # Local trade number format
                    pricing_scope,
                    base_unit_price_fen,
                    unit_price_fen,
                    min_recharge_fen,
                    recharge_step_fen,
                    amount_fen,
                    credits,
                    paid_at,
                ),
            )

            # Insert wallet CHARGE ledger row (task_id=NULL, billing_round=NULL for manual)
            charge_id = f"admin_adjustment:charge:{order_id}"
            conn.execute(
                """
                INSERT INTO wallet_transactions
                (id, user_id, type, available_delta, reserved_delta, recharge_order_id,
                 task_id, billing_round, idempotency_key, auth_source)
                VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s, 'internal')
                """,
                (charge_id, user_id, credits, order_id, charge_id),
            )

        # Atomic wallet balance move — RETURNING the post-update balance so
        # the response never reports a stale pre-read plus credits (a concurrent
        # charge/settle on the same wallet would otherwise be invisible here).
        #
        # 两个方向各有一条 WHERE 护栏，且**刻意不共用**表达式：
        # - 正向 `available_credits <= 2147483647 - credits` 避免先把
        #   `available_credits + credits` 算出来造成 int4 溢出（那会变成资金端点上的
        #   裸 500 —— PR #54 connector review P2）；
        # - 反向 `available_credits + credits >= 0` 是 D2 的余额下限护栏。credits < 0
        #   时这个和不可能超过 int4 上限，所以不会引入同类溢出。
        if is_reversal:
            updated = conn.execute(
                "UPDATE wallets SET available_credits = available_credits + %s "
                "WHERE user_id = %s AND available_credits + %s >= 0 "
                "RETURNING available_credits",
                (credits, user_id, credits),
            ).fetchone()
            refusal = (
                "ADJUSTMENT_BALANCE_INSUFFICIENT",
                "The refund exceeds the customer's available balance: only unspent credits "
                "are refundable, already-consumed credits must be handled off-ledger "
                "(dispute / chargeback channel).",
            )
        else:
            updated = conn.execute(
                "UPDATE wallets SET available_credits = available_credits + %s "
                "WHERE user_id = %s AND available_credits <= 2147483647 - %s "
                "RETURNING available_credits",
                (credits, user_id, credits),
            ).fetchone()
            refusal = (
                "ADJUSTMENT_VALIDATION_FAILED",
                "The credits amount would overflow the wallet balance integer range.",
            )
        if updated is None:
            # Distinguish the three refusal shapes: a wallet that vanished between
            # the existence check and the move (out-of-band maintenance), a
            # forward balance that would overflow the int4 column, and a reverse
            # balance that would go negative (D2 — the DB CHECK stays the
            # defense in depth, this 400 is the operator-facing answer).
            wallet_exists = conn.execute(
                "SELECT 1 FROM wallets WHERE user_id = %s", (user_id,)
            ).fetchone()
            if not wallet_exists:
                raise _http(404, "WALLET_NOT_FOUND", "Wallet not found for target user.")
            raise _http(400, refusal[0], refusal[1])
        balance_after = int(updated[0])

        # Insert audit row (append-only, names the real admin).
        # ``recharge_order_id`` is NULL exactly for reverse adjustments (D4: no
        # recharge order), kept honest by ck_admin_adjustments_order_required.
        conn.execute(
            """
            INSERT INTO admin_adjustments
            (id, recharge_order_id, target_user_id, admin_user_id,
             source_document_type, source_document_ref, reason, request_id, created_at,
             balance_before,balance_after)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                adjustment_id,
                order_id,
                user_id,
                actor.user_id,  # Real acting administrator (§15)
                source_document_type,
                source_document_ref,
                body.reason.strip(),
                request_id,
                now,
                balance_after - credits,
                balance_after,
            ),
        )

        # Log (no sensitive data in logs)
        logger.info(
            "admin adjustment created: adjustment=%s order=%s user=%s "
            "credits=%d direction=%s actor=%s request=%s",
            adjustment_id,
            order_id,
            user_id,
            credits,
            "reversal" if is_reversal else "grant",
            actor.user_id,
            request_id,
        )

        # Return success response. ``order_id`` is "" for reverse adjustments:
        # the field stays a string (no response-shape change) and there is no
        # recharge order to name.
        return {
            "adjustment_id": adjustment_id,
            "order_id": order_id if order_id is not None else "",
            "credits": str(credits),
            "amount_fen": str(amount_fen),
            "pricing_scope": pricing_scope,
            "wallet_balance_after": balance_after,
            "source_document_type": source_document_type,
            "source_document_ref": source_document_ref,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=201)


# ---------------------------------------------------------------------------
# Read-only listing endpoint (audit trail)
# ---------------------------------------------------------------------------


DEFAULT_LIST_LIMIT = 100
MAX_LIST_LIMIT = 200

# 两个调账列表端点共用的连接子句。
#
# ``recharge_orders`` 必须是 LEFT JOIN：反向调账（20260923T1200 起）没有充值单，
# 内连接会把整行审计事实从列表里吃掉——运营在界面上看不到自己刚做的退款。
#
# 台账行同样用 LEFT JOIN 定位：两条通道的流水 id 都是确定式的
# （正向 `admin_adjustment:charge:{order_id}`、反向
# `admin_adjustment:refund:{adjustment_id}`），这里按同一规则反查，因此不需要
# 在审计行上冗余一列流水 id，也不会误配到同客户的其它流水。反向调账的
# credits / amount_fen 只有账本这一个事实源（`available_delta` 为负数）。
_ADJUSTMENT_ORDER_AND_LEDGER_JOINS = """
    LEFT JOIN recharge_orders ro ON ro.id = aa.recharge_order_id
    LEFT JOIN wallet_transactions tx ON tx.id = COALESCE(
        'admin_adjustment:charge:' || aa.recharge_order_id,
        'admin_adjustment:refund:' || aa.id)
"""


@router.get("/customers/{user_id}/adjustments")
def list_admin_adjustments(
    user_id: str,
    actor: AdminReader,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    sort: Literal["asc", "desc"] = "asc",
) -> dict[str, object]:
    """List all adjustments for a target user (audit trail for operators and auditors)."""
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)
    order_by = "aa.created_at DESC, aa.id DESC" if sort == "desc" else "aa.created_at, aa.id"

    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT aa.id, aa.recharge_order_id, aa.admin_user_id,
                       aa.source_document_type, aa.source_document_ref,
                       aa.reason, aa.request_id, aa.created_at,
                       COALESCE(ro.amount_fen, 0), COALESCE(ro.credits, tx.available_delta, 0),
                       COALESCE(ro.pricing_scope, ''), COALESCE(ro.status, ''),
                       admin_user.username, aa.balance_after, aa.balance_before
                FROM admin_adjustments aa
                JOIN users admin_user ON admin_user.id=aa.admin_user_id
                {_ADJUSTMENT_ORDER_AND_LEDGER_JOINS}
                WHERE aa.target_user_id = %s
                ORDER BY {order_by}
                {PAGE_CLAUSE}
                """,  # noqa: S608 -- direction is selected from the Literal above.
                (user_id, bounded_limit, bounded_offset),
            ).fetchall()
            total_row = conn.execute(
                "SELECT COUNT(*) FROM admin_adjustments WHERE target_user_id = %s",
                (user_id,),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "ADJUSTMENT_SERVICE_UNAVAILABLE",
            "Admin adjustments require the PostgreSQL runtime.",
        ) from exc

    items = [
        {
            "adjustment_id": str(row[0]),
            # "" for reverse adjustments (no recharge order — D4); the field
            # stays a string so the response shape is unchanged.
            "order_id": str(row[1]) if row[1] is not None else "",
            "admin_user_id": str(row[2]),
            "source_document_type": str(row[3]),
            "source_document_ref": str(row[4]),
            "reason": str(row[5]),
            "request_id": str(row[6]),
            "created_at": str(row[7]) if row[7] is not None else "",
            "amount_fen": int(row[8]),
            "credits": int(row[9]),
            "pricing_scope": str(row[10]),
            "status": str(row[11]),
            "admin_username": str(row[12]),
            "target_user_id": user_id,
            "balance_after": int(row[13]) if row[13] is not None else None,
            "balance_before": int(row[14]) if row[14] is not None else None,
        }
        for row in rows
    ]

    total = int(total_row[0]) if total_row is not None else 0
    return {"items": items, "total": total, "limit": bounded_limit, "offset": bounded_offset}


@router.get("/adjustments")
def list_all_admin_adjustments(
    actor: AdminReader,
    actor_username: str = "",
    target_username: str = "",
    source_document_type: str = "",
    created_from: str = "",
    created_to: str = "",
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
) -> dict[str, object]:
    """List adjustment records across customers with deterministic ledger balances."""
    del actor
    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)
    clauses: list[str] = []
    params: list[object] = []
    if actor_username.strip():
        clauses.append("admin_user.username ILIKE %s")
        params.append(f"%{actor_username.strip()}%")
    if target_username.strip():
        clauses.append("(target_user.username ILIKE %s OR target_user.display_name ILIKE %s)")
        params.append(f"%{target_username.strip()}%")
        params.append(f"%{target_username.strip()}%")
    if source_document_type.strip():
        clauses.append("aa.source_document_type = %s")
        params.append(source_document_type.strip())
    append_admin_date_filters(
        clauses, params, column="aa.created_at", created_from=created_from, created_to=created_to
    )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    joins = f"""
        FROM admin_adjustments aa
        {_ADJUSTMENT_ORDER_AND_LEDGER_JOINS}
        JOIN users admin_user ON admin_user.id = aa.admin_user_id
        JOIN users target_user ON target_user.id = aa.target_user_id
    """
    try:
        with pg_transaction() as conn:
            rows = conn.execute(
                f"""
                SELECT aa.id, aa.recharge_order_id, aa.admin_user_id,
                       admin_user.username, aa.target_user_id, target_user.username,
                       aa.source_document_type, aa.source_document_ref, aa.reason,
                       aa.request_id, aa.created_at,
                       COALESCE(ro.amount_fen, 0), COALESCE(ro.credits, tx.available_delta, 0),
                       COALESCE(ro.pricing_scope, ''), COALESCE(ro.status, ''),
                        aa.balance_after, aa.balance_before, target_user.display_name
                {joins}
                {where}
                ORDER BY aa.created_at DESC, aa.id DESC {PAGE_CLAUSE}
                """,  # noqa: S608
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            total_row = conn.execute(
                f"SELECT COUNT(*) {joins} {where}",  # noqa: S608
                params,
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "ADJUSTMENT_SERVICE_UNAVAILABLE",
            "Admin adjustments require the PostgreSQL runtime.",
        ) from exc
    return {
        "items": [
            {
                "adjustment_id": str(row[0]),
                # "" for reverse adjustments (no recharge order — D4).
                "order_id": str(row[1]) if row[1] is not None else "",
                "admin_user_id": str(row[2]),
                "admin_username": str(row[3]),
                "target_user_id": str(row[4]),
                "target_username": str(row[5]),
                "target_display_name": str(row[17] or ""),
                "source_document_type": str(row[6]),
                "source_document_ref": str(row[7]),
                "reason": str(row[8]),
                "request_id": str(row[9]),
                "created_at": str(row[10]),
                "amount_fen": int(row[11]),
                "credits": int(row[12]),
                "pricing_scope": str(row[13]),
                "status": str(row[14]),
                "balance_after": None if row[15] is None else int(row[15]),
                "balance_before": None if row[16] is None else int(row[16]),
            }
            for row in rows
        ],
        "total": int(total_row[0]) if total_row else 0,
        "limit": bounded_limit,
        "offset": bounded_offset,
    }


DEFAULT_CUSTOMER_PAGE_SIZE = 20
MAX_CUSTOMER_PAGE_SIZE = 100


CUSTOMER_ACCOUNT_FROM = (
    "FROM users u LEFT JOIN LATERAL ("
    "SELECT u.id AS user_id, COALESCE(a.id, u.id) AS id, "
    "COALESCE(a.activated_at, u.created_at) AS activated_at, a.code_id "
    "FROM (SELECT 1) anchor LEFT JOIN LATERAL ("
    "SELECT binding.id, binding.activated_at, binding.code_id "
    "FROM activation_code_activations binding JOIN activation_codes code "
    "ON code.id = binding.code_id WHERE binding.user_id = u.id "
    "ORDER BY (code.status IN ('ACTIVE', 'SUSPENDED')) DESC, "
    "binding.activated_at DESC, binding.id DESC LIMIT 1"
    ") a ON TRUE) aca ON TRUE "
    "LEFT JOIN activation_codes ac ON ac.id = aca.code_id "
    "LEFT JOIN wallets w ON w.user_id = u.id "
)


@router.get("/adjustments.csv")
def export_adjustments_csv(
    actor: AdminWriter,
    actor_username: str = "",
    target_username: str = "",
    source_document_type: str = "",
    created_from: str = "",
    created_to: str = "",
    limit: int = 5000,
) -> HttpResponse:
    """资金中心·人工调整导出（方案 P1）：与 /adjustments 同筛选口径的整表 CSV。

    复用 customers.csv 的审计与限流模式：导出走 ``control.export`` 高敏审计，
    财务月度对账不再依赖逐页复制。
    """
    import csv as csv_mod
    import hashlib as hashlib_mod
    import io as io_mod

    if _sqlite_lane():
        raise _http(
            503,
            "ADJUSTMENT_SERVICE_UNAVAILABLE",
            "Admin adjustments require the PostgreSQL runtime.",
        )

    try:
        with pg_transaction() as conn:
            decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_CONTROL_EXPORT_ACCOUNT,
                identifier=hashlib_mod.sha256(actor.user_id.encode("utf-8")).hexdigest(),
                limit=control_export_account_limit(),
                window_seconds=rate_limit_window_seconds(),
            )
            if not decision.allowed:
                raise _http(
                    429,
                    "CONTROL_EXPORT_RATE_LIMITED",
                    "Too many ledger exports; retry after the cooldown.",
                )
            clauses: list[str] = []
            params: list[object] = []
            if actor_username.strip():
                clauses.append("admin_user.username ILIKE %s")
                params.append(f"%{actor_username.strip()}%")
            if target_username.strip():
                clauses.append(
                    "(target_user.username ILIKE %s OR target_user.display_name ILIKE %s)"
                )
                params.extend((f"%{target_username.strip()}%", f"%{target_username.strip()}%"))
            if source_document_type.strip():
                clauses.append("aa.source_document_type = %s")
                params.append(source_document_type.strip())
            append_admin_date_filters(
                clauses,
                params,
                column="aa.created_at",
                created_from=created_from,
                created_to=created_to,
            )
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = conn.execute(
                "SELECT aa.created_at, admin_user.username, target_user.username, "
                "aa.source_document_type, aa.source_document_ref, aa.reason, "
                "COALESCE(ro.amount_fen, 0), COALESCE(ro.credits, tx.available_delta, 0) "
                "FROM admin_adjustments aa "
                + _ADJUSTMENT_ORDER_AND_LEDGER_JOINS
                + " JOIN users admin_user ON admin_user.id = aa.admin_user_id "
                + "JOIN users target_user ON target_user.id = aa.target_user_id "
                + f"{where} ORDER BY aa.created_at DESC, aa.id DESC LIMIT %s",
                (*params, max(1, min(limit, 5000))),
            ).fetchall()
            write_audit(
                BusinessConnection.postgres(conn),
                actor=CurrentUser(
                    id=actor.user_id,
                    username=actor.username,
                    display_name=actor.display_name,
                    role=cast(Role, actor.role),
                ),
                action="control.export",
                entity_type="control_ledger",
                entity_id="admin_adjustments",
                metadata={
                    "filters": {
                        "actor_username": actor_username,
                        "target_username": target_username,
                        "source_document_type": source_document_type,
                        "created_from": created_from,
                        "created_to": created_to,
                    },
                    "limit": limit,
                },
            )
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "ADJUSTMENT_SERVICE_UNAVAILABLE",
            "Admin adjustments require the PostgreSQL runtime.",
        ) from exc

    buffer = io_mod.StringIO()
    writer = csv_mod.writer(buffer)
    writer.writerow(
        [
            "created_at",
            "actor_username",
            "target_username",
            "source_document_type",
            "source_document_ref",
            "reason",
            "amount_fen",
            "credits",
        ]
    )
    for row in rows:
        writer.writerow([spreadsheet_safe_cell(str(value)) for value in row])
    payload = buffer.getvalue().encode("utf-8")
    return HttpResponse(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="adjustments.csv"'},
    )


# ---------------------------------------------------------------------------
# 客户暂停 / 恢复（方案 P1 客户管理）：详情页第 4 个主操作。
# 口径（方案待确认问题 #4 的建议项）：暂停只禁止新登录与新任务——
# is_active=0 由登录/会话校验拦截，进行中任务跑完，钱包余额不动；
# 暂停同时吊销当前在线会话（SES-03 传播），恢复不自动登录。
# ---------------------------------------------------------------------------


@router.post("/customers/{user_id}/suspend", status_code=200)
def suspend_customer(
    user_id: str,
    body: AdminWriteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer.suspend",
                reason=body.reason.strip(),
                request_id=request_id,
            )
        reason = body.reason.strip()
        if not reason:
            raise _http(400, "REASON_REQUIRED", "请填写暂停原因。")
        current = conn.execute(
            "SELECT u.is_active FROM users u WHERE u.id = %s FOR UPDATE", (user_id,)
        ).fetchone()
        if current is None:
            raise _http(404, "USER_NOT_FOUND", "客户不存在。")
        if int(current[0]) == 0:
            # 幂等语义：重复暂停不算冲突，返回当前状态（写契约层已去重重放）。
            return {
                "user_id": user_id,
                "is_active": 0,
                "session_revoked": False,
                "request_id": request_id,
            }
        conn.execute("UPDATE users SET is_active = 0 WHERE id = %s", (user_id,))
        revoked = revoke_session(
            conn,
            user_id=user_id,
            actor_user_id=actor.user_id,
            reason=reason,
            request_id=request_id,
            now_iso=_transaction_now_iso(conn),
        )
        # Sub-account admission follows its master. Revoke these sessions too,
        # so resuming the master cannot resurrect a previously paused login.
        for child in conn.execute("SELECT id FROM users WHERE parent_user_id=%s", (user_id,)):
            revoked = (
                revoke_session(
                    conn,
                    user_id=str(child[0]),
                    actor_user_id=actor.user_id,
                    reason=reason,
                    request_id=request_id,
                    now_iso=_transaction_now_iso(conn),
                )
                or revoked
            )
        conn.execute(
            """
            INSERT INTO audit_logs
                (id, actor_user_id, action, entity_type, entity_id, metadata_json)
            VALUES (%s, %s, 'customer.suspend', 'user', %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                user_id,
                json.dumps(
                    {"reason": reason, "request_id": request_id, "session_revoked": revoked},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        logger.info(
            "customer suspended: user=%s actor=%s session_revoked=%s request=%s",
            user_id,
            actor.user_id,
            revoked,
            request_id,
        )
        return {
            "user_id": user_id,
            "is_active": 0,
            "session_revoked": revoked,
            "request_id": request_id,
        }

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/customers/{user_id}/resume", status_code=200)
def resume_customer(
    user_id: str,
    body: AdminWriteRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        if user_id == actor.user_id:
            _deny_admin_self_service(
                conn,
                actor_user_id=actor.user_id,
                target_user_id=user_id,
                attempted_action="customer.resume",
                reason=body.reason.strip(),
                request_id=request_id,
            )
        reason = body.reason.strip()
        if not reason:
            raise _http(400, "REASON_REQUIRED", "请填写恢复原因。")
        current = conn.execute(
            "SELECT u.is_active FROM users u WHERE u.id = %s FOR UPDATE", (user_id,)
        ).fetchone()
        if current is None:
            raise _http(404, "USER_NOT_FOUND", "客户不存在。")
        if int(current[0]) == 1:
            return {
                "user_id": user_id,
                "is_active": 1,
                "request_id": request_id,
            }
        conn.execute("UPDATE users SET is_active = 1 WHERE id = %s", (user_id,))
        conn.execute(
            """
            INSERT INTO audit_logs
                (id, actor_user_id, action, entity_type, entity_id, metadata_json)
            VALUES (%s, %s, 'customer.resume', 'user', %s, %s)
            """,
            (
                str(uuid.uuid4()),
                actor.user_id,
                user_id,
                json.dumps(
                    {"reason": reason, "request_id": request_id},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        return {"user_id": user_id, "is_active": 1, "request_id": request_id}

    return _write_with_idempotency(request, response, actor, body, business, success_status=200)


# 列表排序（方案 P1 客户管理）：经营字段可排序；激活时间为既有默认。
# 本月消耗的「本月」由数据库按上海挂钟取月首，避免应用机时区漂移。
_CUSTOMER_SORTS = {
    "activated": "aca.activated_at DESC, aca.id DESC",
    "recharge": ("COALESCE(recharge.total_fen, 0) DESC, aca.activated_at DESC, aca.id DESC"),
    "month_consumed": ("COALESCE(month_spend.credits, 0) DESC, aca.activated_at DESC, aca.id DESC"),
    "last_active": (
        f"COALESCE(activity.last_at, {utc_text_timestamp('aca.activated_at')}) "
        "DESC, aca.activated_at DESC, aca.id DESC"
    ),
}

# The account admission flag takes precedence over its activation history.
_CUSTOMER_STATUS_SQL = (
    "CASE WHEN u.is_active=0 THEN 'SUSPENDED' ELSE COALESCE(ac.status,'ACTIVE') END"
)


@router.get("/customers")
def list_customers(
    actor: AdminReader,
    limit: int = DEFAULT_CUSTOMER_PAGE_SIZE,
    offset: int = 0,
    username: str = "",
    user_id: str = "",
    status: str = "",
    created_from: str = "",
    created_to: str = "",
    balance_min: int | None = None,
    balance_max: int | None = None,
    sort: Literal["activated", "recharge", "month_consumed", "last_active"] = "activated",
    direction: Literal["asc", "desc"] = "desc",
    attention: CustomerAttention = "",
    low_balance_threshold: int | None = Query(default=None, ge=0, le=2147483647),
) -> dict[str, object]:
    """Registered and activated customer accounts for operators and auditors (ADM-02 read path).

    Users are the account identity; the latest activation is optional. The list carries
    display metadata only — masked code, username, activation time and the
    code status. The identity fields live on users / activation_codes; the
    data model has no customer email, so the T33 contract uses username.

    ``username`` 是「关键字」筛选而非严格用户名筛选：它同时匹配
    ``users.display_name``（客户可填公司名），见下方子句注释。

    A5（2026-09-02 评估）: the page/page_size + ``{customers,…}`` shape is
    retired for the management-wide ``limit/offset`` + ``{items,total,…}``
    envelope, so every admin list paginates the same way.
    """
    bounded_limit, bounded_offset = page_bounds(
        limit, offset, max_limit=MAX_CUSTOMER_PAGE_SIZE, min_limit=1
    )

    clauses: list[str] = [
        "(aca.code_id IS NOT NULL OR (u.role = 'customer' AND "
        "u.registration_source IN ('self_register', 'activation_code')))"
    ]
    params: list[object] = []
    if user_id.strip():
        clauses.append("u.id=%s")
        params.append(user_id.strip())
    if username.strip():
        # 同一个输入同时匹配用户名与公司名称：运营的识别诉求是「这家公司是哪个
        # 账号」，而公司名存在 users.display_name（激活/注册默认写用户名，客户在
        # 个人中心改成公司名）。只匹配 u.username 会让按公司名搜索必然零结果。
        clauses.append("(u.username ILIKE %s OR u.display_name ILIKE %s)")
        # Escape LIKE wildcards so a value containing % or _ is matched
        # literally (PostgreSQL LIKE treats backslash as the default escape).
        literal = username.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{literal}%")
        params.append(f"%{literal}%")
    if status.strip():
        clauses.append(f"{_CUSTOMER_STATUS_SQL} = %s")
        params.append(status.strip().upper())
    append_admin_date_filters(
        clauses, params, column="aca.activated_at", created_from=created_from, created_to=created_to
    )
    if balance_min is not None:
        clauses.append("COALESCE(w.available_credits, 0) >= %s")
        params.append(max(0, balance_min))
    if balance_max is not None:
        clauses.append("COALESCE(w.available_credits, 0) <= %s")
        params.append(max(0, balance_max))

    summary_clauses = list(clauses)
    summary_params = list(params)
    append_attention_filter(clauses, params, attention, low_balance_threshold)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    order_sql = _CUSTOMER_SORTS[sort]
    if direction == "asc":
        order_sql = order_sql.replace(" DESC", " ASC")
    try:
        with pg_transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            rows = conn.execute(
                CUSTOMER_FACTS_CTE
                + "SELECT aca.user_id, u.username, u.display_name, aca.activated_at, "
                "COALESCE(ac.id, ''), COALESCE(ac.masked_code, '账号注册'), "
                f"{_CUSTOMER_STATUS_SQL}, "
                "COALESCE(w.available_credits, 0), COALESCE(w.reserved_credits, 0), "
                "COALESCE(devices.slots_used, 0), "
                "u.max_devices, "
                "COALESCE(usage.generation_total, 0) + COALESCE(oral_usage.total, 0), "
                "COALESCE(usage.generation_succeeded, 0) + COALESCE(oral_usage.succeeded, 0), "
                "COALESCE(usage.generation_failed, 0) + COALESCE(oral_usage.failed, 0), "
                "COALESCE(usage.generation_in_progress, 0) + COALESCE(oral_usage.in_progress, 0), "
                "COALESCE(usage.generation_attention, 0) + COALESCE(oral_usage.attention, 0), "
                "COALESCE(spend.credits_spent, 0), "
                "COALESCE(annotation.tags_json, '[]'::jsonb), "
                "COALESCE(annotation.note, ''), "
                "COALESCE(annotation.owner_user_id, ''), "
                "COALESCE(owner.username, ''), "
                # 经营字段（方案 P1 客户管理）：累计充值 / 最近活跃 / 当前权益。
                "COALESCE(recharge.total_fen, 0), "
                "activity.last_at, "
                "COALESCE(month_spend.credits, 0), "
                "benefit.discount_rate, u.is_active, ac.status, "
                "COALESCE(recent.total_30d,0), COALESCE(recent.succeeded_30d,0), "
                "COALESCE(recent.failed_30d,0), COALESCE(recent.failed_7d,0) "
                + CUSTOMER_ACCOUNT_FROM
                + "LEFT JOIN (SELECT user_id, COUNT(*) AS slots_used FROM customer_devices "
                "  WHERE status = 'BOUND' GROUP BY user_id) devices "
                "  ON devices.user_id = aca.user_id "
                "LEFT JOIN ("
                "  SELECT gb.created_by_user_id AS user_id, "
                "    COUNT(*) AS generation_total, "
                "    COUNT(*) FILTER (WHERE gt.status = 'SUCCEEDED' "
                "      AND gt.archive_status IN ('ARCHIVED', 'DIRECT')) AS generation_succeeded, "
                "    COUNT(*) FILTER (WHERE gt.status IN ('FAILED', 'CANCELLED')) "
                "      AS generation_failed, "
                "    COUNT(*) FILTER (WHERE gt.status = 'SUBMISSION_UNCERTAIN' "
                "      OR gt.archive_status = 'ARCHIVE_FAILED' "
                "      OR gt.quality_status IN ("
                "        'AUDIO_QUALITY_FAILED', 'VISUAL_QUALITY_FAILED'"
                "      )) AS generation_attention, "
                "    COUNT(*) FILTER (WHERE NOT ("
                "      gt.status = 'SUCCEEDED' AND gt.archive_status IN ('ARCHIVED', 'DIRECT')"
                "    ) AND gt.status NOT IN ('FAILED', 'CANCELLED', 'SUBMISSION_UNCERTAIN') "
                "      AND gt.archive_status != 'ARCHIVE_FAILED' "
                "      AND gt.quality_status NOT IN ("
                "        'AUDIO_QUALITY_FAILED', 'VISUAL_QUALITY_FAILED'"
                "      )) "
                "      AS generation_in_progress "
                "  FROM generation_batches gb "
                "  JOIN generation_tasks gt ON gt.batch_id = gb.id "
                "  GROUP BY gb.created_by_user_id"
                ") usage ON usage.user_id = aca.user_id "
                "LEFT JOIN (SELECT owner_user_id AS user_id, COUNT(*) AS total, "
                " COUNT(*) FILTER (WHERE status = 'SUCCEEDED') AS succeeded, "
                " COUNT(*) FILTER (WHERE status IN ('FAILED', 'CANCELLED')) AS failed, "
                " COUNT(*) FILTER (WHERE status IN "
                " ('QUEUED', 'SUBMITTING', 'RUNNING', 'ARCHIVING')) AS in_progress, "
                " COUNT(*) FILTER (WHERE status IN "
                " ('SUBMISSION_UNCERTAIN', 'ARCHIVE_FAILED')) AS attention "
                " FROM oral_tasks GROUP BY owner_user_id) oral_usage "
                " ON oral_usage.user_id = aca.user_id "
                "LEFT JOIN ("
                "  SELECT user_id, COALESCE(SUM(-reserved_delta), 0) AS credits_spent "
                "  FROM wallet_transactions WHERE type = 'SETTLE' GROUP BY user_id"
                ") spend ON spend.user_id = aca.user_id "
                # 客户标注（方案 P2-3）：标签/备注/负责人。空标注不落行，LEFT JOIN
                # 天然给出零值；负责人是管理员账号，取用户名供列表直接展示。
                "LEFT JOIN customer_annotations annotation "
                "  ON annotation.user_id = aca.user_id "
                "LEFT JOIN users owner ON owner.id = annotation.owner_user_id "
                # 累计充值：实付订单（线下开通同样落 PAID 订单）。
                "LEFT JOIN ("
                "  SELECT user_id, COALESCE(SUM(amount_fen), 0) AS total_fen "
                f"  FROM recharge_orders ro WHERE status = 'PAID' AND {CASH_ORDER_ELIGIBLE} "
                "GROUP BY user_id"
                ") recharge ON recharge.user_id = aca.user_id "
                + CUSTOMER_METRICS_JOINS
                # 本月消耗：上海挂钟月首之后的净扣积分。
                + "LEFT JOIN ("
                "  SELECT user_id, COALESCE(SUM(-reserved_delta), 0) AS credits "
                "  FROM wallet_transactions WHERE type = 'SETTLE' "
                "    AND (created_at::timestamp AT TIME ZONE 'UTC') >= "
                "        date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai') "
                "        AT TIME ZONE 'Asia/Shanghai' "
                "  GROUP BY user_id"
                ") month_spend ON month_spend.user_id = aca.user_id "
                # 当前权益：生效中的专项折扣；费率原值返回，折文案在 Python 侧
                # 换算（0.85 → 8.5 折），SQL 里拼字符串会把进制搞混。
                "LEFT JOIN ("
                "  SELECT d.user_id, MIN(d.discount_rate) AS discount_rate "
                "  FROM customer_discounts d WHERE d.is_active GROUP BY d.user_id"
                ") benefit ON benefit.user_id = aca.user_id "
                f"{where} "
                f"ORDER BY {order_sql} "
                f"{PAGE_CLAUSE}",
                (*params, bounded_limit, bounded_offset),
            ).fetchall()
            total_row = conn.execute(
                CUSTOMER_FACTS_CTE
                + "SELECT COUNT(*) "
                + CUSTOMER_ACCOUNT_FROM
                + CUSTOMER_METRICS_JOINS
                + f"{where}",
                params,
            ).fetchone()
            summary = conn.execute(
                CUSTOMER_FACTS_CTE + "SELECT "
                "COUNT(*) FILTER (WHERE %s::integer IS NOT NULL "
                "AND COALESCE(w.available_credits,0)<%s), "
                "COUNT(*) FILTER (WHERE COALESCE(recent.failed_7d,0)>0), "
                f"COUNT(*) FILTER (WHERE {INACTIVE_SQL}) "
                + CUSTOMER_ACCOUNT_FROM
                + CUSTOMER_METRICS_JOINS
                + f"WHERE {' AND '.join(summary_clauses)}",
                (low_balance_threshold, low_balance_threshold, *summary_params),
            ).fetchone()
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "CUSTOMER_SERVICE_UNAVAILABLE",
            "Customer management requires the PostgreSQL runtime.",
        ) from exc

    customers = [
        {
            "user_id": str(row[0]),
            "username": str(row[1]),
            "display_name": str(row[2]),
            "created_at": str(row[3]) if row[3] is not None else "",
            "activation_code_id": str(row[4]),
            "activation_code": str(row[5]),
            "status": str(row[6]),
            "account_active": bool(row[25]),
            "activation_status": str(row[26]) if row[26] is not None else None,
            "available_credits": int(row[7]),
            "reserved_credits": int(row[8]),
            "device_slots_used": int(row[9]),
            "device_slots_total": None,
            "generation_total": int(row[11]),
            "generation_succeeded": int(row[12]),
            "generation_failed": int(row[13]),
            "generation_in_progress": int(row[14]),
            "generation_attention": int(row[15]),
            "credits_spent": int(row[16]),
            # 标注三件套（方案 P2-3）：无行即零值，前端不需要区分「没标过」与
            # 「被清空」。tags_json 正常已是 list，坏值降级为空列表。
            "tags": [str(tag) for tag in row[17]] if isinstance(row[17], list) else [],
            "note": str(row[18]),
            "owner_user_id": str(row[19]),
            "owner_username": str(row[20]),
            # 经营字段（方案 P1）：列表直接展示，不再进详情页逐个翻。
            "total_recharge_fen": int(row[21]),
            "last_active_at": (
                row[22].isoformat() if row[22] is not None and hasattr(row[22], "isoformat") else ""
            ),
            "month_consumed_credits": int(row[23]),
            "generation_total_30d": int(row[27]),
            "generation_succeeded_30d": int(row[28]),
            "generation_failed_30d": int(row[29]),
            "generation_failed_7d": int(row[30]),
            "success_rate_30d": (100 * int(row[28]) / int(row[27]) if row[27] else None),
            "low_balance": (
                int(row[7]) < low_balance_threshold if low_balance_threshold is not None else None
            ),
            "current_benefit": (
                # 0.85 → 「专项 8.5 折」；无生效折扣显示空串，前端落「原价」。
                f"专项 {Decimal(str(row[24])) * 10:g} 折" if row[24] is not None else ""
            ),
        }
        for row in rows
    ]
    total = int(total_row[0]) if total_row is not None else 0
    summary = summary or (0, 0, 0)
    return {
        "items": customers,
        "total": total,
        "limit": bounded_limit,
        "offset": bounded_offset,
        "attention_counts": {
            "low_balance": int(summary[0]) if low_balance_threshold is not None else None,
            "recent_failure": int(summary[1]),
            "inactive": int(summary[2]),
        },
        "low_balance_threshold": low_balance_threshold,
        "attention_basis": "当前名称/日期/状态/余额筛选的全量客户；不受分页和所选待办类别影响",
    }


@router.get("/customers/{user_id}/overview")
def customer_overview(user_id: str, actor: AdminReader) -> dict[str, object]:
    """Thirty Shanghai calendar days of ledger consumption and five mixed factual events."""
    del actor
    with pg_transaction(isolation="REPEATABLE READ") as conn:
        found = conn.execute(
            "SELECT 1 FROM users u WHERE u.id=%s AND (u.role='customer' "
            "OR EXISTS (SELECT 1 FROM activation_code_activations a WHERE a.user_id=u.id))",
            (user_id,),
        ).fetchone()
        if found is None:
            raise _http(404, "USER_NOT_FOUND", "客户不存在。")
        trend = conn.execute(
            f"""
            SELECT days.day::date,
              COALESCE(SUM(-tx.reserved_delta) FILTER (WHERE tx.type='SETTLE'),0)
            FROM generate_series(
              (now() AT TIME ZONE 'Asia/Shanghai')::date - 29,
              (now() AT TIME ZONE 'Asia/Shanghai')::date, interval '1 day') days(day)
            LEFT JOIN wallet_transactions tx ON tx.user_id=%s
              AND ({utc_text_timestamp("tx.created_at")} AT TIME ZONE 'Asia/Shanghai')::date
                = days.day::date
              AND {utc_text_timestamp("tx.created_at")} <= now()
            GROUP BY days.day ORDER BY days.day
            """,
            (user_id,),
        ).fetchall()
        events = conn.execute(
            CUSTOMER_FACTS_CTE
            + f"""
            SELECT kind, id, at, label, credits FROM (
              SELECT 'recharge'::text AS kind, ro.id,
                {utc_text_timestamp("ro.paid_at")} AS at,
                '充值到账'::text AS label, ro.credits::bigint AS credits
              FROM recharge_orders ro WHERE ro.user_id=%s AND ro.status='PAID'
                AND ro.amount_fen>0 AND ro.paid_at IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM admin_adjustments aa WHERE aa.recharge_order_id=ro.id)
              UNION ALL
              SELECT 'generation', id, created_at,
                label || CASE WHEN succeeded THEN ' · 成功'
                  WHEN failed THEN ' · 失败/待处理' ELSE ' · 处理中' END, NULL::bigint
              FROM customer_task_facts WHERE user_id=%s
              UNION ALL
              SELECT 'adjustment', aa.id, {utc_text_timestamp("aa.created_at")}, '人工调整',
                COALESCE(ro.credits, tx.available_delta)::bigint
              FROM admin_adjustments aa
              LEFT JOIN recharge_orders ro ON ro.id=aa.recharge_order_id
              LEFT JOIN wallet_transactions tx ON tx.id=COALESCE(
                'admin_adjustment:charge:' || aa.recharge_order_id,
                'admin_adjustment:refund:' || aa.id)
              WHERE aa.target_user_id=%s
              UNION ALL
              SELECT 'login', id, created_at::timestamptz, '客户登录', NULL::bigint
              FROM customer_session_events WHERE user_id=%s AND event='LOGIN'
            ) events WHERE at<=now()
            ORDER BY at DESC, kind, id DESC LIMIT 5
            """,
            (user_id, user_id, user_id, user_id),
        ).fetchall()
    return {
        "user_id": user_id,
        "timezone": "Asia/Shanghai",
        "daily_consumption": [{"day": row[0].isoformat(), "credits": int(row[1])} for row in trend],
        "timeline": [
            {
                "kind": row[0],
                "event_id": str(row[1]),
                "created_at": row[2].isoformat(),
                "label": row[3],
                "credits": int(row[4]) if row[4] is not None else None,
            }
            for row in events
        ],
        "basis": (
            "近30个北京时间自然日，按原账本SETTLE实扣积分；"
            "最近5条按时间混排充值/生成/调整/登录，不含未发生事实。"
        ),
    }


@router.get("/customers.csv")
def export_customers_csv(
    actor: AdminWriter,
    status: str | None = None,
    username: str = "",
    created_from: str = "",
    created_to: str = "",
    balance_min: int | None = None,
    balance_max: int | None = None,
    attention: CustomerAttention = "",
    low_balance_threshold: int | None = Query(default=None, ge=0, le=2147483647),
    sort: Literal["activated", "recharge", "month_consumed", "last_active"] = "activated",
    direction: Literal["asc", "desc"] = "desc",
    limit: int = 5000,
) -> HttpResponse:
    """Export the customer list as CSV (C6) — audited + rate limited (A2).

    Replaces the console's client-side "current page only" export: the whole
    (filtered) list leaves through one audited dump with the same columns the
    operator saw in the table — 含公司名称（``display_name``），否则按公司名
    识别出的客户在导出件里又失去对应关系。``username`` 与列表端点同为关键字，
    同时匹配用户名与公司名称。
    """
    import csv as csv_mod
    import hashlib as hashlib_mod
    import io as io_mod

    if _sqlite_lane():
        raise _http(
            503,
            "CUSTOMER_SERVICE_UNAVAILABLE",
            "Customer management requires the PostgreSQL runtime.",
        )

    try:
        with pg_transaction() as conn:
            decision = consume_rate_limit(
                conn,
                dimension=DIMENSION_CONTROL_EXPORT_ACCOUNT,
                identifier=hashlib_mod.sha256(actor.user_id.encode("utf-8")).hexdigest(),
                limit=control_export_account_limit(),
                window_seconds=rate_limit_window_seconds(),
            )
            if not decision.allowed:
                raise _http(
                    429,
                    "CONTROL_EXPORT_RATE_LIMITED",
                    "Too many ledger exports; retry after the cooldown.",
                )
            clauses: list[str] = [
                "(aca.code_id IS NOT NULL OR (u.role = 'customer' AND "
                "u.registration_source IN ('self_register', 'activation_code')))"
            ]
            params: list[object] = []
            if username.strip():
                # 与列表端点同口径：关键字同时匹配用户名与公司名称，否则
                # 「按公司名筛出客户再导出」会得到只有表头的空 CSV。
                literal = (
                    username.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                clauses.append("(u.username ILIKE %s OR u.display_name ILIKE %s)")
                params.append(f"%{literal}%")
                params.append(f"%{literal}%")
            # PR #85 review P2: the UI dropdown sends lowercase status values
            # while the database enum is uppercase — normalize server-side so
            # every caller (not just this console) matches real rows instead
            # of exporting a header-only CSV.
            normalized_status = status.strip().upper() if status else ""
            if normalized_status:
                clauses.append(f"{_CUSTOMER_STATUS_SQL} = %s")
                params.append(normalized_status)
            append_admin_date_filters(
                clauses,
                params,
                column="aca.activated_at",
                created_from=created_from,
                created_to=created_to,
            )
            if balance_min is not None:
                clauses.append("COALESCE(w.available_credits, 0) >= %s")
                params.append(max(0, balance_min))
            if balance_max is not None:
                clauses.append("COALESCE(w.available_credits, 0) <= %s")
                params.append(max(0, balance_max))
            append_attention_filter(clauses, params, attention, low_balance_threshold)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            # Same list sort/filter, including customer-level attention across all pages.
            export_joins = (
                CUSTOMER_METRICS_JOINS
                + f"""
              LEFT JOIN (SELECT user_id, SUM(amount_fen) AS total_fen
                FROM recharge_orders ro WHERE status='PAID' AND {CASH_ORDER_ELIGIBLE}
                GROUP BY user_id) recharge
                ON recharge.user_id=u.id
              LEFT JOIN (SELECT user_id, SUM(-reserved_delta) AS credits
                FROM wallet_transactions WHERE type='SETTLE'
                  AND created_at::timestamp AT TIME ZONE 'UTC' >=
                    date_trunc('month', now() AT TIME ZONE 'Asia/Shanghai')
                    AT TIME ZONE 'Asia/Shanghai' GROUP BY user_id) month_spend
                ON month_spend.user_id=u.id
            """
            )
            order_sql = _CUSTOMER_SORTS[sort]
            if direction == "asc":
                order_sql = order_sql.replace(" DESC", " ASC")
            rows = conn.execute(
                CUSTOMER_FACTS_CTE + "SELECT u.username, u.display_name, "
                "COALESCE(ac.masked_code, '账号注册'), aca.activated_at, "
                f"{_CUSTOMER_STATUS_SQL} "
                + CUSTOMER_ACCOUNT_FROM
                + export_joins
                + f"{where} ORDER BY {order_sql} LIMIT %s",
                (*params, max(1, min(limit, 5000))),
            ).fetchall()
            write_audit(
                BusinessConnection.postgres(conn),
                actor=CurrentUser(
                    id=actor.user_id,
                    username=actor.username,
                    display_name=actor.display_name,
                    role=cast(Role, actor.role),
                ),
                action="control.export",
                entity_type="control_ledger",
                entity_id="customers",
                metadata={
                    "filters": {
                        "status": normalized_status,
                        "username": username,
                        "created_from": created_from,
                        "created_to": created_to,
                        "balance_min": balance_min,
                        "balance_max": balance_max,
                        "attention": attention,
                        "low_balance_threshold": low_balance_threshold,
                        "sort": sort,
                        "direction": direction,
                    },
                    "limit": limit,
                },
            )
    except (RuntimeError, MissingDatabaseConfigError) as exc:
        raise _http(
            503,
            "CUSTOMER_SERVICE_UNAVAILABLE",
            "Customer management requires the PostgreSQL runtime.",
        ) from exc

    buffer = io_mod.StringIO()
    writer = csv_mod.writer(buffer)
    writer.writerow(["username", "display_name", "masked_code", "activated_at", "status"])
    for row in rows:
        writer.writerow([spreadsheet_safe_cell(str(value)) for value in row])
    payload = buffer.getvalue().encode("utf-8")
    return HttpResponse(
        content=payload,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="customers.csv"'},
    )
