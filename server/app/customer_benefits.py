"""客户权益的管理端写入 — 手工折扣（单客户定价个案）.

需求（2026-09-27 用户确认）：

- 管理员可为单个客户直接设置折扣（不挂套餐），与「按套餐代客开通」并列。
- 手工折扣优先于套餐折扣（方案 A）：``MANUAL_DISCOUNT_PRIORITY`` 高于
  ``recharge_packages.PACKAGE_DISCOUNT_PRIORITY``，计费侧 ``get_best_discount`` 按优先级取首行，
  因此客户之后再买套餐也不会把管理员谈定的价格顶掉；停用手工折扣即回落到套餐折扣。
- 同一客户同时只保留一条生效的手工折扣：新设置停用旧手工行。同优先级多行并存时
  ``get_best_discount`` 只能按 id 兜底排序，运营无法预判哪条生效——与套餐的
  「最近覆盖」对称，让结果可解释。套餐来源行永不被手工写入触碰，反之亦然。

本模块是纯服务层；调用方负责管理写契约（幂等 + 审计 + 事务）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, cast
from uuid import uuid4

import psycopg

from app.discount_service import validate_discount_rate
from app.recharge_packages import normalize_interfaces

# 方案 A：必须高于 PACKAGE_DISCOUNT_PRIORITY（测试钉住这一不等式）。
MANUAL_DISCOUNT_PRIORITY = 200
_RATE_QUANTUM = Decimal("0.0001")

# 客户详情只需要近期权益；套餐反复覆盖会累积停用行，上限防止详情页无界读取。
MAX_LISTED_DISCOUNTS = 50


class DiscountNotFound(Exception):
    """折扣不存在或不属于该客户。"""


class DiscountNotManual(Exception):
    """套餐来源的折扣由「最近覆盖」管理，不允许手工停用。"""


@dataclass(frozen=True)
class CustomerDiscountView:
    id: str
    discount_rate: Decimal
    applicable_interfaces: tuple[str, ...]
    priority: int
    is_active: bool
    valid_from: datetime
    valid_until: datetime | None
    source: str  # "manual" | "recharge_package"
    source_recharge_order_id: str | None
    package_name: str | None
    created_at: datetime
    state: Literal["effective", "pending", "expired", "disabled"]


def lock_customer_discounts(conn: psycopg.Connection, user_id: str) -> None:
    """与 ``grant_package_discount`` 同一把锁：手工写入与套餐授予对同一客户串行."""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('billing:customer-discount:' || %s))",
        (user_id,),
    )


def list_customer_discounts(conn: psycopg.Connection, user_id: str) -> list[CustomerDiscountView]:
    rows = conn.execute(
        "SELECT d.id, d.discount_rate, d.applicable_interfaces, d.priority, d.is_active, "
        "d.valid_from, d.valid_until, d.source_recharge_order_id, d.created_at, "
        "o.package_snapshot_json, "
        "CASE WHEN NOT d.is_active THEN 'disabled' "
        "WHEN d.valid_from > discount_clock.at_time THEN 'pending' "
        "WHEN d.valid_until IS NOT NULL AND d.valid_until <= discount_clock.at_time "
        "THEN 'expired' ELSE 'effective' END "
        "FROM customer_discounts d "
        "CROSS JOIN (SELECT clock_timestamp() AS at_time) discount_clock "
        "LEFT JOIN recharge_orders o ON o.id = d.source_recharge_order_id "
        "WHERE d.user_id = %s "
        "ORDER BY d.is_active DESC, d.created_at DESC, d.id "
        "LIMIT %s",
        (user_id, MAX_LISTED_DISCOUNTS),
    ).fetchall()
    return [_row_to_view(row) for row in rows]


def create_manual_discount(
    conn: psycopg.Connection,
    *,
    user_id: str,
    discount_rate: object,
    applicable_interfaces: Sequence[str],
    valid_until: datetime | None,
    actor_user_id: str,
) -> tuple[str, list[str]]:
    """新建手工折扣并停用该客户此前生效的手工折扣；返回 ``(新 id, 被停用的 id)``.

    调用方须已持有 ``lock_customer_discounts``。``valid_until`` 的「必须晚于现在」
    由数据库时钟判定（SES-01），不信任应用墙钟。
    """
    rate = validate_discount_rate(discount_rate).quantize(_RATE_QUANTUM)
    # 量化可能把很小的正数变成零，必须在停用旧权益之前拒绝。
    validate_discount_rate(rate)
    interfaces = normalize_interfaces(list(applicable_interfaces))
    if valid_until is not None:
        row = conn.execute("SELECT %s > clock_timestamp()", (valid_until,)).fetchone()
        if row is None or not row[0]:
            raise ValueError("到期时间必须晚于当前时间")
    replaced = [
        str(row[0])
        for row in conn.execute(
            "UPDATE customer_discounts SET is_active = false, updated_at = clock_timestamp() "
            "WHERE user_id = %s AND source_recharge_order_id IS NULL AND is_active "
            "RETURNING id",
            (user_id,),
        ).fetchall()
    ]
    discount_id = f"manual-discount-{uuid4().hex}"
    conn.execute(
        "INSERT INTO customer_discounts (id, user_id, discount_rate, priority, valid_until, "
        "applicable_interfaces, is_active, created_by_user_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, true, %s)",
        (
            discount_id,
            user_id,
            rate,
            MANUAL_DISCOUNT_PRIORITY,
            valid_until,
            json.dumps(interfaces),
            actor_user_id,
        ),
    )
    return discount_id, replaced


def deactivate_manual_discount(conn: psycopg.Connection, *, user_id: str, discount_id: str) -> bool:
    """停用一条手工折扣；返回停用前是否生效（已停用的重复请求不报错，便于重试）."""
    row = conn.execute(
        "SELECT is_active, source_recharge_order_id FROM customer_discounts "
        "WHERE id = %s AND user_id = %s FOR UPDATE",
        (discount_id, user_id),
    ).fetchone()
    if row is None:
        raise DiscountNotFound(discount_id)
    if row[1] is not None:
        raise DiscountNotManual(discount_id)
    was_active = bool(row[0])
    if was_active:
        conn.execute(
            "UPDATE customer_discounts SET is_active = false, updated_at = clock_timestamp() "
            "WHERE id = %s",
            (discount_id,),
        )
    return was_active


def _row_to_view(row: Sequence[object]) -> CustomerDiscountView:
    source_order_id = None if row[7] is None else str(row[7])
    package_name: str | None = None
    if row[9]:
        try:
            snapshot = json.loads(str(row[9]))
        except ValueError:
            snapshot = None
        if isinstance(snapshot, dict) and snapshot.get("name") is not None:
            package_name = str(snapshot["name"])
    raw_rate = row[1]
    raw_interfaces = row[2]
    try:
        loaded = json.loads(str(raw_interfaces)) if raw_interfaces else []
    except ValueError:
        loaded = []
    interfaces = tuple(str(item) for item in loaded) if isinstance(loaded, list) else ()
    valid_from = row[5]
    valid_until = row[6]
    created_at = row[8]
    assert isinstance(valid_from, datetime) and isinstance(created_at, datetime)
    assert valid_until is None or isinstance(valid_until, datetime)
    return CustomerDiscountView(
        id=str(row[0]),
        discount_rate=raw_rate if isinstance(raw_rate, Decimal) else Decimal(str(raw_rate)),
        applicable_interfaces=interfaces,
        priority=int(cast(int, row[3])),
        is_active=bool(row[4]),
        valid_from=valid_from,
        valid_until=valid_until,
        source="manual" if source_order_id is None else "recharge_package",
        source_recharge_order_id=source_order_id,
        package_name=package_name,
        created_at=created_at,
        state=cast(Literal["effective", "pending", "expired", "disabled"], str(row[10])),
    )
