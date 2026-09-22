"""CW-075 — 折扣数据模型的基础读取层（客户计费基础设施）.

为 CW-076「折扣计算 + reserve/finalize 改造」提供数据访问底座：读取客户当前生效的
消耗侧折扣、优先级互斥取最高、适用接口范围匹配、折扣率边界校验与折后价计算。

红线（§0.3 / §2.3）：

- **R-A 成本泄露面**：折扣是售价侧概念（``user_price_fen × discount_rate``），本模块
  绝不触碰 ``cost_price_fen``；折后价计算只作用于售价分位。
- **R-C 管理写契约**：折扣配置的写入是管理端操作（confirm + reason + Idempotency-Key +
  audit_logs），本模块只提供**只读**查询；写路径归后续管理端任务。
- **R-D 历史账目冻结**：客户泳道只读已冻结的 ``discount_rate_snapshot``（CW-076 在
  reserve 时落），改配置不重算历史。
- **多折扣源互斥取优先级最高**（§2.3）：``priority`` 数值越大越优先，``get_best_discount``
  取排序首行。

PostgreSQL 是客户泳道唯一真源，且运行时**无 ORM**（db_pg.py：psycopg3 + psycopg_pool
only），故每个 PG 入口都期望一条活的 ``psycopg.Connection``（沿 api_key_service 先例）。

状态（BILLING-OBS P1-3，2026-09）：本模块仍无生产调用点——消耗侧客户折扣**未接入**
reserve/finalize（CW-076）。当前唯一生效的折扣是 ``customer_credit_pricing.config_json``
的全局 ``discount_basis_points``（管理端「客户报价配置」页可编辑，受理时冻结进快照）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

import psycopg


@dataclass(frozen=True)
class DiscountRecord:
    """``customer_discounts`` 一行——客户消耗侧折扣配置视图（只读）.

    ``source_recharge_order_id`` 非空表示该折扣由充值套餐授予（充值时自动写入）；
    ``None`` 表示管理员手工折扣——套餐「最近覆盖」逻辑不触碰手工折扣。
    """

    id: str
    user_id: str
    discount_rate: Decimal
    priority: int
    valid_from: datetime
    valid_until: datetime | None
    applicable_interfaces: tuple[str, ...]
    is_active: bool
    source_recharge_order_id: str | None = None


# ---------------------------------------------------------------------------
# 纯函数：折扣率校验 + 折后价计算（无 PG，可本地全绿覆盖）
# ---------------------------------------------------------------------------


def validate_discount_rate(rate: object) -> Decimal:
    """校验折扣率落在 ``0 < rate <= 1.0``，归一为 ``Decimal`` 返回.

    接受 ``Decimal`` / ``float`` / ``str``；其余类型 ``TypeError``。越界 ``ValueError``。
    ``float`` 经 ``str`` 中转避免二进制浮点误差（``Decimal(str(0.85)) == Decimal("0.85")``）。
    """
    if isinstance(rate, Decimal):
        value = rate
    elif isinstance(rate, float):
        value = Decimal(str(rate))
    elif isinstance(rate, str):
        try:
            value = Decimal(rate)
        except InvalidOperation as exc:
            raise ValueError(f"discount_rate string is not a valid decimal: {rate!r}") from exc
    else:
        raise TypeError("discount_rate must be Decimal/float/str")
    if value <= 0:
        raise ValueError("discount_rate must be > 0")
    if value > 1:
        raise ValueError("discount_rate must be <= 1.0")
    return value


def calculate_discounted_price(original_price_fen: int, discount_rate: Decimal | None) -> int:
    """折后售价（分），``ceil`` 向上取整确保平台不损失分位精度（§2.3）.

    ``discount_rate=None`` 表示无折扣，原样返回 ``original_price_fen``。向上取整是消耗侧
    折扣的既定口径（``reserve`` 时 ``ceil(billed_seconds × discount_rate)``），宁多收一分
    也不少收，避免累计侵蚀。仅作用于售价，绝不涉及成本（R-A）。
    """
    if discount_rate is None:
        return original_price_fen
    rate = validate_discount_rate(discount_rate)
    discounted = Decimal(original_price_fen) * rate
    return int(discounted.to_integral_value(rounding=ROUND_CEILING))


# ---------------------------------------------------------------------------
# PG 读取：活跃折扣查询 / 互斥取最高 / 折扣率解析
# ---------------------------------------------------------------------------


def _parse_interfaces(raw: str | None) -> tuple[str, ...]:
    """解析 ``applicable_interfaces`` TEXT-JSON 数组；空/非法一律空元组（= 全部接口）。"""
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded)


def _row_to_record(row: tuple[Any, ...]) -> DiscountRecord:
    rate = row[2]
    return DiscountRecord(
        id=str(row[0]),
        user_id=str(row[1]),
        discount_rate=rate if isinstance(rate, Decimal) else Decimal(str(rate)),
        priority=int(row[3]),
        valid_from=row[4],
        valid_until=row[5],
        applicable_interfaces=_parse_interfaces(row[6]),
        is_active=bool(row[7]),
        source_recharge_order_id=(str(row[8]) if len(row) > 8 and row[8] is not None else None),
    )


def get_active_discounts(
    conn: psycopg.Connection,
    *,
    user_id: str,
    interface_key: str | None = None,
    at_time: datetime | None = None,
) -> list[DiscountRecord]:
    """查询用户当前有效的活跃折扣，按 ``priority DESC``（互斥取最高）、``id ASC``（稳定次序）.

    有效期过滤在 SQL 侧完成（``valid_from <= now`` 且 ``valid_until IS NULL OR > now``）；
    接口范围过滤在应用侧（``applicable_interfaces`` 是 TEXT-JSON，空数组 = 全部接口）。
    ``at_time`` 默认取当前时刻——生产调用方应传 ``pg_server_now()``（SES-01 唯一可信时钟），
    不要传本地墙钟。
    """
    now = at_time or datetime.now(UTC)
    rows = conn.execute(
        "SELECT id, user_id, discount_rate, priority, valid_from, valid_until, "
        "applicable_interfaces, is_active, source_recharge_order_id "
        "FROM customer_discounts "
        "WHERE user_id = %s AND is_active AND valid_from <= %s "
        "AND (valid_until IS NULL OR valid_until > %s) "
        "ORDER BY priority DESC, id ASC",
        (user_id, now, now),
    ).fetchall()
    records = [_row_to_record(row) for row in rows]
    if interface_key is None:
        return records
    return [
        record
        for record in records
        if not record.applicable_interfaces or interface_key in record.applicable_interfaces
    ]


def get_best_discount(
    conn: psycopg.Connection,
    *,
    user_id: str,
    interface_key: str | None = None,
    at_time: datetime | None = None,
) -> DiscountRecord | None:
    """互斥取优先级最高的折扣（§2.3）；无有效折扣返回 ``None``（走原价）。"""
    discounts = get_active_discounts(
        conn, user_id=user_id, interface_key=interface_key, at_time=at_time
    )
    return discounts[0] if discounts else None


def get_discount_rate(
    conn: psycopg.Connection,
    *,
    user_id: str,
    interface_key: str | None = None,
    at_time: datetime | None = None,
) -> Decimal | None:
    """用户当前生效的折扣率；无折扣返回 ``None``（reserve 侧按原价，CW-076 消费）。"""
    best = get_best_discount(conn, user_id=user_id, interface_key=interface_key, at_time=at_time)
    return best.discount_rate if best is not None else None
