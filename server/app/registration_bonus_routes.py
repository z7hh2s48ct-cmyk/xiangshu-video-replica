"""注册赠送积分：管理端配置读写 + 注册路径的一次性发放。

「价格与套餐」设置页维护 ``registration_bonus_settings`` 单行表的
``bonus_credits``（0 = 关闭）；``POST /api/customer/register`` 在创建账号与
钱包的同一事务里调用 :func:`grant_registration_bonus` 发放。两个入口读写
同一行配置，行为只有一份口径：

- 仅覆盖此后**新注册**的主账号——注册端点只创建 MASTER，子账号由主账号
  自建且没有独立钱包，天然不在发放范围；已注册账号不补发。
- 发放落账复用 FREE_GRANT 的审计形状（054 已放行 0 元 ``admin_adjustment``
  单）：PAID 订单 + CHARGE 流水 + 钱包自增，三行同事务。不写
  ``admin_adjustments``——它的 ``admin_user_id`` 要求真实管理员，系统按策略
  自动发放没有操作者，留痕靠订单号（``REGBONUS-{user_id}``）、流水幂等键
  与本模块设置页的 ``audit_logs``。
"""

from __future__ import annotations

import json
import logging
import uuid

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import transaction_now_iso, write_with_idempotency
from app.api_errors import http_error
from app.db_pg import pg_transaction

router = APIRouter(prefix="/api/control", tags=["admin-registration-bonus"])
logger = logging.getLogger(__name__)

_UNAVAILABLE_CODE = "REGISTRATION_BONUS_SETTINGS_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "注册赠送设置需要 PostgreSQL 运行时。"

# 注册路径行缺失时与注册端点同 code：对客户侧这就是「注册服务暂不可用」，
# 不暴露内部哪张配置表缺行。
_REGISTRATION_UNAVAILABLE_CODE = "REGISTRATION_SERVICE_UNAVAILABLE"

_SETTINGS_SELECT = (
    "SELECT s.bonus_credits, s.updated_by_user_id, s.updated_at, u.display_name "
    "FROM registration_bonus_settings AS s "
    "LEFT JOIN users AS u ON u.id = s.updated_by_user_id "
    "WHERE s.id = 1"
)


def _iso_timestamp(value: object) -> str | None:
    """timestamptz 列 → ISO 文本；None 透传。"""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class RegistrationBonusSettingsSnapshot(BaseModel):
    """注册赠送积分当前值 + 最近修改（设置页展示）。"""

    model_config = ConfigDict(extra="forbid")

    bonus_credits: int
    updated_by_user_id: str | None
    updated_by_display_name: str | None
    updated_at: str | None


class RegistrationBonusSettingsUpdate(AdminWriteRequest):
    """全量更新注册赠送积分；范围与迁移 CHECK 同口径。"""

    model_config = ConfigDict(extra="forbid")

    bonus_credits: StrictInt = Field(ge=0, le=2147483647)


def _load_snapshot(conn: psycopg.Connection) -> RegistrationBonusSettingsSnapshot:
    """设置快照；行缺失 fail-closed——配置页不展示假状态。"""
    row = conn.execute(_SETTINGS_SELECT).fetchone()
    if row is None:
        raise http_error(503, _UNAVAILABLE_CODE, "注册赠送设置行缺失，请先执行数据库迁移。")
    return RegistrationBonusSettingsSnapshot(
        bonus_credits=int(row[0]),
        updated_by_user_id=None if row[1] is None else str(row[1]),
        updated_by_display_name=None if row[3] is None else str(row[3]),
        updated_at=_iso_timestamp(row[2]),
    )


@router.get("/settings/registration-bonus", response_model=RegistrationBonusSettingsSnapshot)
def read_registration_bonus_settings(
    response: Response, _actor: AdminReader
) -> RegistrationBonusSettingsSnapshot:
    """注册赠送积分快照（「价格与套餐」页设置区块）。"""
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as conn:
        return _load_snapshot(conn)


@router.put("/settings/registration-bonus", response_model=RegistrationBonusSettingsSnapshot)
def update_registration_bonus_settings(
    body: RegistrationBonusSettingsUpdate,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    """全量更新注册赠送积分；只影响此后新注册的主账号，不补发存量。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        before = _load_snapshot(conn)
        conn.execute(
            "UPDATE registration_bonus_settings SET bonus_credits = %s, "
            "updated_by_user_id = %s, updated_at = clock_timestamp() WHERE id = 1",
            (body.bonus_credits, actor.user_id),
        )
        after = _load_snapshot(conn)
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'registration_bonus.settings.update', "
            "'registration_bonus_settings', '1', %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "old": before.model_dump(),
                        "new": after.model_dump(),
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "registration bonus settings updated: credits=%s actor=%s request=%s",
            after.bonus_credits,
            actor.user_id,
            request_id,
        )
        return after.model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )


def grant_registration_bonus(conn: psycopg.Connection, user_id: str) -> int:
    """按配置给刚注册的主账号发放一次性积分；返回实际发放数（0 = 未发放）。

    必须在注册账号 + 钱包的同一事务内调用：发放失败整个注册回滚，绝不
    产出「注册成功但没积分」的半态。落账形状与 FREE_GRANT 审计调账一致
    （PAID 0 元 ``admin_adjustment`` 单 + CHARGE 流水 + 钱包自增），定价
    快照取 ``runtime_settings`` 的 INTERNAL 口径——注册用户是全新账号，
    既无激活绑定也无客户单价，scope 恒为 INTERNAL。
    """
    row = conn.execute(
        "SELECT bonus_credits FROM registration_bonus_settings WHERE id = 1"
    ).fetchone()
    if row is None:
        # 种子行由迁移保证存在；缺行意味着部署异常，fail-closed 拒绝注册，
        # 与「PG 不可用时 503」同一风格，而不是静默跳过赠送。
        raise http_error(
            503,
            _REGISTRATION_UNAVAILABLE_CODE,
            "Customer registration requires the PostgreSQL runtime.",
        )
    bonus = int(row[0])
    if bonus <= 0:
        return 0

    snapshot = conn.execute(
        "SELECT internal_base_unit_price_fen, min_recharge_fen, recharge_step_fen "
        "FROM runtime_settings WHERE id = 1"
    ).fetchone()
    if snapshot is None:
        raise http_error(
            503,
            _REGISTRATION_UNAVAILABLE_CODE,
            "Customer registration requires the PostgreSQL runtime.",
        )
    base_unit_price_fen = int(snapshot[0])

    order_id = str(uuid.uuid4())
    paid_at = transaction_now_iso(conn)
    # 本地单号直接绑定 user_id：一个账号至多注册成功一次，天然唯一；
    # 0 元单的 provider 必须是 admin_adjustment（054 的 CHECK 放行口径）。
    conn.execute(
        """
        INSERT INTO recharge_orders
        (id, user_id, merchant_order_no, provider, status, pricing_scope,
         base_unit_price_fen_snapshot, charged_unit_price_fen_snapshot,
         min_recharge_fen_snapshot, recharge_step_fen_snapshot,
         amount_fen, credits, paid_at)
        VALUES (%s, %s, %s, 'admin_adjustment', 'PAID', 'INTERNAL',
                %s, %s, %s, %s, 0, %s, %s)
        """,
        (
            order_id,
            user_id,
            f"REGBONUS-{user_id}",
            base_unit_price_fen,
            base_unit_price_fen,
            int(snapshot[1]),
            int(snapshot[2]),
            bonus,
            paid_at,
        ),
    )
    charge_id = f"registration_bonus:charge:{order_id}"
    conn.execute(
        """
        INSERT INTO wallet_transactions
        (id, user_id, type, available_delta, reserved_delta, recharge_order_id,
         task_id, billing_round, idempotency_key, auth_source)
        VALUES (%s, %s, 'CHARGE', %s, 0, %s, NULL, NULL, %s, 'internal')
        """,
        (charge_id, user_id, bonus, order_id, charge_id),
    )
    # 新钱包从 0 起步且 bonus ≤ int4 上限（迁移 CHECK），护栏按理不会拦；照抄
    # 审计调账的防溢出写法，防御将来注册路径复用到非零初始钱包的场景。
    updated = conn.execute(
        "UPDATE wallets SET available_credits = available_credits + %s "
        "WHERE user_id = %s AND available_credits <= 2147483647 - %s "
        "RETURNING available_credits",
        (bonus, user_id, bonus),
    ).fetchone()
    if updated is None:
        raise http_error(
            503,
            _REGISTRATION_UNAVAILABLE_CODE,
            "Customer registration requires the PostgreSQL runtime.",
        )
    logger.info(
        "registration bonus granted: user=%s credits=%d order=%s",
        user_id,
        bonus,
        order_id,
    )
    return bonus
