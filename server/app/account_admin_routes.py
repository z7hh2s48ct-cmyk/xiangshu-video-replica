"""Native administrator account operations; never the legacy proxy identity."""

import json
from dataclasses import asdict
from typing import Any, Literal, cast
from uuid import uuid4

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

import app.wechat_native_provider  # noqa: F401 -- register the selectable provider
from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import (
    AdminWriteContract,
    require_write_contract,
    write_with_idempotency,
)
from app.api_key_routes import ApiKeyRecordResponse
from app.api_key_service import list_api_keys
from app.auth import CurrentUser, Role
from app.control_routes import (
    BillingSettingsSnapshot,
    BillingSettingsUpdate,
    ControlRechargeOrderPage,
    ControlWalletTransactionPage,
    MaskedZPaySettings,
    OrderStatus,
    TransactionType,
    ZPaySettingsUpdate,
    _update_control_billing_settings_business,
    _update_control_zpay_settings_business,
    list_recharge_orders,
    list_wallet_transactions,
)
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.payment_provider import PaymentProviderError, get_payment_provider
from app.settings import SettingsRepository
from app.zpay_payments import (
    WECHAT_NATIVE_SETTLEMENT_SPEC,
    ZPAY_SETTLEMENT_SPEC,
    PaymentConfirmationError,
    confirm_recharge_payment,
    read_recharge_order,
    serialize_recharge_order,
)

router = APIRouter(prefix="/api/control", tags=["account-operations"])


class H3AccountUpdate(AdminWriteContract):
    name: str = Field(min_length=1, max_length=80)
    api_key: str = Field(default="", repr=False)
    concurrency_limit: int = Field(ge=1, le=1_000_000, strict=True)
    enabled: bool = True
    expected_version: int = Field(ge=0)


@router.get("/settings/h3-accounts")
def get_h3_accounts(actor: AdminReader, response: Response) -> dict[str, Any]:
    from app.h3_account_pool import read_accounts

    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as raw:
        return read_accounts(BusinessConnection.postgres(raw))


@router.put("/settings/h3-accounts/{account_id}")
def put_h3_account(
    account_id: str, body: H3AccountUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, Any]:
    from app.h3_account_pool import save_account

    if not account_id or len(account_id) > 80:
        raise HTTPException(422, detail="账号标识无效。")
    # Avoid validation errors echoing an oversized credential as the input value.
    if len(body.api_key) > 8192:
        raise HTTPException(422, detail="API Key 长度无效。")
    response.headers["Cache-Control"] = "no-store"

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn = BusinessConnection.postgres(raw)
        result = save_account(
            conn,
            account_id=account_id,
            name=body.name,
            api_key=body.api_key,
            concurrency_limit=body.concurrency_limit,
            enabled=body.enabled,
            expected_version=body.expected_version,
        )
        _payment_audit(
            conn,
            actor_id=actor.user_id,
            action="h3.account.update",
            entity_type="h3_provider_account",
            entity_id=account_id,
            reason=body.reason,
            request_id=request_id,
            details={
                "account_id": account_id,
                "concurrency_limit": body.concurrency_limit,
                "enabled": body.enabled,
                "key_updated": bool(body.api_key.strip()),
            },
        )
        return result

    return write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.post("/customers/{user_id}/recharge-orders/{order_no}/reconcile")
def reconcile_order(
    user_id: str,
    order_no: str,
    body: AdminWriteContract,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    require_write_contract(request, body)
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        original = read_recharge_order(conn, merchant_order_no=order_no)
        if original is None or str(original["user_id"]) != user_id:
            raise HTTPException(404, detail="该账号下的充值订单不存在。")
        provider_name = str(original["provider"])
        if provider_name not in {"zpay", "wechat_native"}:
            raise HTTPException(409, detail="此订单不支持支付网关补发。")
        paid = str(original["status"]) == "PAID"
        gateway = get_payment_provider(provider_name)
        try:
            merchant = None if paid else gateway.load_merchant_config(conn)
        except ValueError as exc:
            raise HTTPException(503, detail="支付渠道配置暂不可用。") from exc
    # No database connection or wallet lock is held during the external request.
    remote = None
    if merchant is not None:
        try:
            remote = gateway.query_order(
                merchant=merchant,
                deployment=gateway.load_deployment_config(),
                merchant_order_no=order_no,
            )
        except (ValueError, PaymentProviderError) as exc:
            raise HTTPException(502, detail="支付网关暂时无法核验，请稍后重试。") from exc
        if not remote.paid:
            raise HTTPException(409, detail="网关尚未确认支付成功，未增加积分。")
        if (
            remote.merchant_order_no != order_no
            or not remote.provider_trade_no
            or remote.amount_fen is None
            or remote.channel is None
        ):
            raise HTTPException(502, detail="网关支付凭证不完整，未增加积分。")

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        # Recheck authority after the potentially slow provider call and hold it until commit.
        authority = raw.execute(
            "SELECT s.id FROM admin_sessions s JOIN users u ON u.id = s.actor_user_id "
            "WHERE s.id = %s AND u.id = %s AND s.revoked_at IS NULL "
            "AND s.auth_method = 'password' AND u.is_active = 1 AND "
            "u.role IN ('admin', 'operator') "
            "AND s.expires_at::timestamptz > clock_timestamp() FOR SHARE OF s, u",
            (actor.session_id, actor.user_id),
        ).fetchone()
        if authority is None:
            raise HTTPException(403, detail="管理员权限已变化，请重新登录。")
        conn = BusinessConnection.postgres(raw)
        current = read_recharge_order(conn, merchant_order_no=order_no)
        if (
            current is None
            or str(current["user_id"]) != user_id
            or str(current["provider"]) != provider_name
        ):
            raise HTTPException(409, detail="充值订单归属已变化。")
        if remote is not None and merchant is not None:
            try:
                current = confirm_recharge_payment(
                    conn,
                    merchant_order_no=order_no,
                    provider_trade_no=remote.provider_trade_no or "",
                    amount_fen=remote.amount_fen or 0,
                    channel=remote.channel or "",
                    source_digest=remote.response_digest,
                    allowed_channels=merchant.allowed_channels,
                    provider_spec=ZPAY_SETTLEMENT_SPEC
                    if provider_name == "zpay"
                    else WECHAT_NATIVE_SETTLEMENT_SPEC,
                )
            except PaymentConfirmationError as exc:
                raise HTTPException(
                    exc.status_code, detail={"code": exc.code, "message": str(exc)}
                ) from exc
        if str(current["status"]) != "PAID":
            raise HTTPException(409, detail="订单未确认支付成功。")
        conn.execute(
            "INSERT INTO audit_logs (id, actor_user_id, action, "
            "entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'account_payment.reconcile', 'recharge_order', %s, %s)",
            (
                str(uuid4()),
                actor.user_id,
                str(current["id"]),
                json.dumps({"reason": body.reason, "request_id": request_id, "user_id": user_id}),
            ),
        )
        return dict(serialize_recharge_order(current))

    return write_with_idempotency(request, response, actor, body, business, success_status=200)


class AccountSummary(BaseModel):
    user_id: str
    available_credits: int
    reserved_credits: int
    total_consumed_credits: int
    software_consumed_credits: int
    other_consumed_credits: int
    tokens: list[ApiKeyRecordResponse]


@router.get("/customers/{user_id}/account-summary", response_model=AccountSummary)
def account_summary(user_id: str, _actor: AdminReader, response: Response) -> AccountSummary:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        wallet = conn.execute(
            "SELECT w.available_credits, w.reserved_credits FROM wallets w JOIN users u "
            "ON u.id = w.user_id WHERE u.id = %s AND (u.role = 'customer' OR EXISTS "
            "(SELECT 1 FROM activation_code_activations a WHERE a.user_id = u.id))",
            (user_id,),
        ).fetchone()
        if wallet is None:
            raise HTTPException(404, detail="客户账号不存在。")
        spend = conn.execute(
            "SELECT COALESCE(SUM(-reserved_delta), 0), "
            "COALESCE(SUM(-reserved_delta) FILTER (WHERE auth_source = 'session'), 0), "
            "COALESCE(SUM(-reserved_delta) FILTER (WHERE api_key_id IS NULL "
            "AND auth_source IS DISTINCT FROM 'session'), 0) "
            "FROM wallet_transactions WHERE user_id = %s AND type = 'SETTLE'",
            (user_id,),
        ).fetchone()
        assert spend is not None
        return AccountSummary(
            user_id=user_id,
            available_credits=wallet[0],
            reserved_credits=wallet[1],
            total_consumed_credits=spend[0],
            software_consumed_credits=spend[1],
            other_consumed_credits=spend[2],
            tokens=[
                ApiKeyRecordResponse.model_validate(asdict(k))
                for k in list_api_keys(conn, user_id=user_id)
            ],
        )


class CustomerPaymentSettings(BaseModel):
    billing: BillingSettingsSnapshot
    zpay: MaskedZPaySettings
    wechat_native: dict[str, Any]
    active_provider: str
    deployment: dict[str, object]


def _payment_deployment_status(provider_name: str) -> dict[str, object]:
    """Saving preferences is independent of deployment; checkout still validates it."""
    try:
        get_payment_provider(provider_name).load_deployment_config()
    except (ValueError, KeyError):
        return {
            "ready": False,
            "message": "配置可保存；充值尚未就绪，请在服务端设置 HTTPS 回调域名 PUBLIC_BASE_URL。",
        }
    return {"ready": True, "message": ""}


@router.get("/settings/customer-payments", response_model=CustomerPaymentSettings)
def payment_settings(_actor: AdminReader, response: Response) -> CustomerPaymentSettings:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        repo = SettingsRepository(BusinessConnection.postgres(conn))
        active_provider = repo.read_active_payment_provider()
        return CustomerPaymentSettings(
            billing=BillingSettingsSnapshot(**repo.read_billing_settings()),
            zpay=MaskedZPaySettings(**repo.read_zpay_config()),
            wechat_native=repo.read_wechat_native_config(),
            active_provider=active_provider,
            deployment=_payment_deployment_status(active_provider),
        )


class WeChatSettingsUpdate(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    config: dict[str, str] = Field(default_factory=dict)


class PaymentProviderUpdate(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    active_provider: Literal["zpay", "wechat_native"]
    zpay: ZPaySettingsUpdate | None = Field(default=None, repr=False)
    wechat_native: dict[str, str] | None = Field(default=None, repr=False)


def _payment_audit(
    conn: BusinessConnection,
    *,
    actor_id: str,
    action: str,
    reason: str,
    request_id: str,
    details: dict[str, object],
    entity_type: str = "payment_settings",
    entity_id: str = "default",
) -> None:
    conn.execute(
        "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            str(uuid4()),
            actor_id,
            action,
            entity_type,
            entity_id,
            json.dumps({"reason": reason, "request_id": request_id, **details}),
        ),
    )


@router.post("/settings/customer-payments/wechat-native/self-check")
def self_check_wechat_settings(
    request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    """Probe WeChat once with the saved credentials (mchid + serial_no + private
    key must sign acceptably, api_v3_key must decrypt the answer). Read-only:
    nothing is saved and the shared certificate cache is left alone, so a
    misconfiguration surfaces here instead of at the first real customer order.
    """
    response.headers["Cache-Control"] = "no-store"
    from app.wechat_native_client import (
        PlatformCertificateManager,
        WeChatNativeClient,
        WeChatNativeError,
        merchant_config_from_settings,
    )

    with pg_transaction() as raw:
        settings = SettingsRepository(BusinessConnection.postgres(raw)).load_wechat_native_config()
    try:
        merchant = merchant_config_from_settings(settings)
    except ValueError as exc:
        return {"ok": False, "code": "WECHAT_CONFIG_INVALID", "message": str(exc)}
    if merchant.public_key_id:
        # 公钥模式的商户没有平台证书可下载（微信回“无可用的平台证书”），改用
        # 一次必然查无此单的查询来证明签名三件套被微信接受。
        try:
            WeChatNativeClient().check_public_key_credentials(merchant)
        except WeChatNativeError as exc:
            return {"ok": False, "code": "WECHAT_SELF_CHECK_FAILED", "message": str(exc)}
        return {
            "ok": True,
            "code": None,
            "message": "商户凭据有效：签名被微信接受（微信支付公钥模式）。",
            "verification_mode": "public_key",
        }
    try:
        certificates = PlatformCertificateManager().check_credentials(merchant)
    except WeChatNativeError as exc:
        return {"ok": False, "code": "WECHAT_SELF_CHECK_FAILED", "message": str(exc)}
    return {
        "ok": True,
        "code": None,
        "message": "商户凭据有效：签名被微信接受，平台证书解密成功。",
        "verification_mode": "platform_certificate",
        "platform_certificates": certificates,
    }


@router.patch("/settings/customer-payments/wechat-native")
def save_wechat_settings(
    body: WeChatSettingsUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn = BusinessConnection.postgres(raw)
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('payment:settings'))")
        try:
            result = SettingsRepository(conn).save_wechat_native_config(
                body.config, actor_user_id=actor.user_id
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                422,
                detail=(
                    "微信商户配置无效：请检查必填项、32 字节 API v3 密钥及 RSA 私钥；"
                    "微信支付公钥 ID（PUB_KEY_ID_ 开头）与公钥须成对填写；"
                    "有待支付订单时不能更换商户号或 AppID。"
                ),
            ) from exc
        _payment_audit(
            conn,
            actor_id=actor.user_id,
            action="payment.wechat.update",
            reason=body.reason,
            request_id=request_id,
            details={"changed_fields": sorted(body.config)},
        )
        return result

    return write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.patch("/settings/customer-payments/provider")
def save_payment_provider(
    body: PaymentProviderUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"

    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        conn = BusinessConnection.postgres(raw)
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('payment:settings'))")
        repo = SettingsRepository(conn)
        if (body.active_provider == "zpay" and body.wechat_native is not None) or (
            body.active_provider == "wechat_native" and body.zpay is not None
        ):
            raise HTTPException(422, detail="商户配置与所选支付通道不一致。")
        if body.zpay is not None:
            _update_control_zpay_settings_business(
                conn,
                actor=CurrentUser(
                    id=actor.user_id,
                    username=actor.username,
                    display_name=actor.display_name,
                    role=cast(Role, actor.role),
                ),
                payload=body.zpay.model_copy(update={"reason": body.reason}),
                request_id=request_id,
            )
        if body.wechat_native is not None:
            try:
                repo.save_wechat_native_config(body.wechat_native, actor_user_id=actor.user_id)
            except (ValueError, TypeError) as exc:
                raise HTTPException(
                    422,
                    detail=(
                        "微信商户配置无效，请检查商户信息、API v3 密钥和私钥；"
                        "微信支付公钥 ID 与公钥须成对填写；"
                        "待支付订单未结束时不能更换商户身份。"
                    ),
                ) from exc
            _payment_audit(
                conn,
                actor_id=actor.user_id,
                action="payment.wechat.update",
                reason=body.reason,
                request_id=request_id,
                details={"changed_fields": sorted(body.wechat_native)},
            )
        try:
            provider = get_payment_provider(body.active_provider)
            provider.load_merchant_config(conn)
        except (ValueError, KeyError) as exc:
            raise HTTPException(
                422,
                detail="商户配置不完整，请填写所选通道的商户信息；已保存的密钥可留空保留。",
            ) from exc
        previous = repo.read_active_payment_provider()
        conn.execute(
            "INSERT INTO runtime_settings(id,active_payment_provider) VALUES(1,%s) "
            "ON CONFLICT(id) DO UPDATE SET "
            "active_payment_provider=excluded.active_payment_provider",
            (body.active_provider,),
        )
        _payment_audit(
            conn,
            actor_id=actor.user_id,
            action="payment.provider.update",
            reason=body.reason,
            request_id=request_id,
            details={"previous": previous, "active_provider": body.active_provider},
        )
        return {
            "active_provider": body.active_provider,
            "zpay": repo.read_zpay_config(),
            "wechat_native": repo.read_wechat_native_config(),
            "deployment": _payment_deployment_status(body.active_provider),
        }

    return write_with_idempotency(request, response, actor, body, business, success_status=200)


@router.patch("/settings/customer-payments/billing", response_model=BillingSettingsSnapshot)
def save_billing(
    body: BillingSettingsUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        lambda conn, request_id: _update_control_billing_settings_business(
            BusinessConnection.postgres(conn),
            actor=CurrentUser(
                id=actor.user_id,
                username=actor.username,
                display_name=actor.display_name,
                role=cast(Role, actor.role),
            ),
            payload=body,
            request_id=request_id,
        ),
        success_status=200,
    )


@router.patch("/settings/customer-payments/zpay", response_model=MaskedZPaySettings)
def save_zpay(
    body: ZPaySettingsUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    response.headers["Cache-Control"] = "no-store"
    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        lambda conn, request_id: _update_control_zpay_settings_business(
            BusinessConnection.postgres(conn),
            actor=CurrentUser(
                id=actor.user_id,
                username=actor.username,
                display_name=actor.display_name,
                role=cast(Role, actor.role),
            ),
            payload=body,
            request_id=request_id,
        ),
        success_status=200,
    )


@router.get("/customers/{user_id}/recharge-orders", response_model=ControlRechargeOrderPage)
def account_recharge_orders(
    user_id: str,
    actor: AdminReader,
    response: Response,
    status: OrderStatus | None = None,
    username: str | None = None,
    channel: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlRechargeOrderPage:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as raw:
        return list_recharge_orders(
            conn=BusinessConnection.postgres(raw),
            _actor=CurrentUser(
                id=actor.user_id,
                username=actor.username,
                display_name=actor.display_name,
                role=cast(Role, actor.role),
            ),
            user_id=user_id,
            status=status,
            username=username,
            channel=channel,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )


@router.get("/customers/{user_id}/wallet-transactions", response_model=ControlWalletTransactionPage)
def account_wallet_transactions(
    user_id: str,
    actor: AdminReader,
    response: Response,
    type: TransactionType | None = None,
    username: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> ControlWalletTransactionPage:
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as raw:
        return list_wallet_transactions(
            conn=BusinessConnection.postgres(raw),
            _actor=CurrentUser(
                id=actor.user_id,
                username=actor.username,
                display_name=actor.display_name,
                role=cast(Role, actor.role),
            ),
            user_id=user_id,
            type=type,
            username=username,
            created_from=created_from,
            created_to=created_to,
            limit=limit,
            offset=offset,
        )
