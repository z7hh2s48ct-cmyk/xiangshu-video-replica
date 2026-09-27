"""充值套餐 — 管理员配置档位（赠送积分 + 消耗侧折扣权益）.

需求口径（2026-09-22 用户确认）：

- 管理员配置档位：充值金额、到账积分（可含赠送，如 1998 元 → 2000 积分）、
  折扣率与适用接口范围（如视频生成 9 折）。朴素档位（无折扣、无赠送口径差异）同样合法。
- 折扣永久有效（``valid_until = NULL``），授予时写入 ``customer_discounts``。
- 带权益套餐「最近覆盖」：同一用户的新套餐权益行会停用旧的**套餐来源**行；
  管理员手工折扣（``source_recharge_order_id IS NULL``）永不被套餐触碰。
- 无权益套餐（``discount_rate IS NULL``）是纯赠送/换算档位，不触碰用户已有权益。
- 自定义金额（不选套餐）保留：走基础汇率换算，无权益。

本模块是**纯服务层**（无 FastAPI 依赖）：管理端路由与客户只读路由都调用它；
写入必须在 ``fenced_pg_transaction`` + advisory lock 内由调用方包好（R-C 管理写契约）。

PostgreSQL 是客户泳道唯一真源，且运行时无 ORM（db_pg.py：psycopg3 only），
故每个 PG 入口都期望 ``BusinessConnection``（沿 discount_service / customer_pricing 先例）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from app.billing_catalog import INTERFACE_KEYS
from app.db_portable import BusinessConnection
from app.discount_service import validate_discount_rate

# 套餐权益的折扣优先级：管理端手工折扣固定用更高的 customer_benefits.MANUAL_DISCOUNT_PRIORITY，
# 单客户个案定价因此始终压过套餐权益（get_active_discounts 按 priority DESC 取优）。
PACKAGE_DISCOUNT_PRIORITY = 100

# 金额/积分为 PG Integer，值域 1 .. 2147483647（与迁移 CHECK 一致）。
MAX_INT4 = 2_147_483_647
_RATE_QUANTUM = Decimal("0.0001")

_SELECT_COLUMNS = (
    "id, name, amount_fen, credits, discount_rate, discount_interfaces, sort_order, "
    "is_active, version, created_at, updated_at"
)


class PackageNotFound(Exception):
    """套餐不存在（管理端更新时给出明确 404 语义）。"""


class PackageVersionConflict(Exception):
    """乐观锁冲突：``expected_version`` 与库内版本不一致，须重读后重试。"""


@dataclass(frozen=True)
class RechargePackage:
    """``recharge_packages`` 一行——套餐配置视图."""

    id: str
    name: str
    amount_fen: int
    credits: int
    discount_rate: Decimal | None
    discount_interfaces: tuple[str, ...]
    sort_order: int
    is_active: bool
    version: int
    created_at: datetime | None
    updated_at: datetime | None


# ---------------------------------------------------------------------------
# 纯函数：草稿校验 / 接口键归一 / 快照编解码（无 PG，本地全绿覆盖）
# ---------------------------------------------------------------------------


def normalize_interfaces(interfaces: object) -> list[str]:
    """归一折扣接口键：去重保序；未知键显式拒绝（配置笔误不得静默落库）."""
    if interfaces is None:
        return []
    if isinstance(interfaces, (str, bytes)) or not isinstance(interfaces, Iterable):
        raise ValueError("折扣接口范围必须是数组")
    keys: list[str] = []
    for raw in interfaces:
        key = str(raw).strip()
        if not key:
            raise ValueError("折扣接口键不能为空")
        if key not in INTERFACE_KEYS:
            raise ValueError(f"未知接口键：{key}")
        if key not in keys:
            keys.append(key)
    return keys


def parse_interfaces_json(raw: str | None) -> tuple[str, ...]:
    """容错解析 ``discount_interfaces`` TEXT-JSON；空/非法一律空元组（= 全部接口）.

    读取已落库的行用容错解析（历史数据不得让读取方崩溃）；写入侧走
    ``normalize_interfaces`` 严格校验。
    """
    if not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded)


def validate_package_draft(
    *,
    name: str,
    amount_fen: int,
    credits: int,
    discount_rate: object | None,
    discount_interfaces: object = None,
) -> tuple[Decimal | None, list[str]]:
    """校验套餐草稿并归一折扣（率 4 位小数量化 + 接口键去重）.

    返回 ``(归一后的折扣率或 None, 归一后的接口键列表)`` 供写入方复用。
    拒绝：空名称 / 金额或积分越界 / 折扣率越界 / 无折扣却勾接口 / 未知接口键。
    """
    if not str(name or "").strip():
        raise ValueError("套餐名称不能为空")
    if not _is_int4(amount_fen, minimum=1):
        raise ValueError("充值金额必须为 1 到 2147483647 之间的整数分")
    if not _is_int4(credits, minimum=1):
        raise ValueError("到账积分必须为 1 到 2147483647 之间的整数")
    rate: Decimal | None = None
    if discount_rate is not None:
        rate = validate_discount_rate(discount_rate).quantize(_RATE_QUANTUM)
    interfaces = normalize_interfaces(discount_interfaces)
    if rate is None and interfaces:
        raise ValueError("未配置折扣时不能设置折扣接口范围")
    return rate, interfaces


def build_package_snapshot(package: RechargePackage) -> dict[str, object]:
    """下单时冻结套餐内容（R-D）：事后改套餐不重算历史订单/权益."""
    return {
        "package_id": package.id,
        "name": package.name,
        "amount_fen": package.amount_fen,
        "credits": package.credits,
        "discount_rate": str(package.discount_rate) if package.discount_rate is not None else None,
        "discount_interfaces": list(package.discount_interfaces),
    }


def parse_package_snapshot(raw: str | None) -> dict[str, object] | None:
    """解析 ``recharge_orders.package_snapshot_json``；空/非法/非对象返回 None."""
    if not raw:
        return None
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _is_int4(value: object, *, minimum: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return minimum <= value <= MAX_INT4


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _row_to_package(row: Any) -> RechargePackage:
    return RechargePackage(
        id=str(row["id"]),
        name=str(row["name"]),
        amount_fen=int(row["amount_fen"]),
        credits=int(row["credits"]),
        discount_rate=_as_decimal(row["discount_rate"]),
        discount_interfaces=parse_interfaces_json(row["discount_interfaces"]),
        sort_order=int(row["sort_order"]),
        is_active=bool(row["is_active"]),
        version=int(row["version"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# PG 服务：CRUD（管理端）与权益授予（充值结算）
# ---------------------------------------------------------------------------


def list_packages(conn: BusinessConnection, *, active_only: bool = False) -> list[RechargePackage]:
    """按 ``sort_order, id`` 稳定排序列出套餐；``active_only`` 供客户侧只读."""
    sql = f"SELECT {_SELECT_COLUMNS} FROM recharge_packages"
    if active_only:
        sql += " WHERE is_active"
    sql += " ORDER BY sort_order, id"
    return [_row_to_package(row) for row in conn.execute(sql).fetchall()]


def read_package(conn: BusinessConnection, package_id: str) -> RechargePackage | None:
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM recharge_packages WHERE id = %s", (package_id,)
    ).fetchone()
    return _row_to_package(row) if row is not None else None


def create_package(
    conn: BusinessConnection,
    *,
    name: str,
    amount_fen: int,
    credits: int,
    discount_rate: object | None,
    discount_interfaces: Sequence[str] | None = None,
    sort_order: int = 0,
    is_active: bool = True,
    actor_user_id: str | None = None,
) -> RechargePackage:
    """新建套餐；调用方负责管理写契约（advisory lock + 幂等 + 审计）."""
    rate, interfaces = validate_package_draft(
        name=name,
        amount_fen=amount_fen,
        credits=credits,
        discount_rate=discount_rate,
        discount_interfaces=discount_interfaces,
    )
    package_id = f"rpkg-{uuid4().hex}"
    conn.execute(
        "INSERT INTO recharge_packages (id, name, amount_fen, credits, discount_rate, "
        "discount_interfaces, sort_order, is_active, version, created_by_user_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0, %s)",
        (
            package_id,
            str(name).strip(),
            amount_fen,
            credits,
            rate,
            json.dumps(interfaces),
            int(sort_order),
            bool(is_active),
            actor_user_id,
        ),
    )
    record = read_package(conn, package_id)
    assert record is not None  # 刚插入的行必然可读
    return record


def update_package(
    conn: BusinessConnection,
    *,
    package_id: str,
    expected_version: int,
    name: str,
    amount_fen: int,
    credits: int,
    discount_rate: object | None,
    discount_interfaces: Sequence[str] | None = None,
    sort_order: int = 0,
    is_active: bool = True,
    actor_user_id: str | None = None,
) -> RechargePackage:
    """更新套餐（乐观锁 ``expected_version``）；版本冲突 / 不存在显式抛错."""
    rate, interfaces = validate_package_draft(
        name=name,
        amount_fen=amount_fen,
        credits=credits,
        discount_rate=discount_rate,
        discount_interfaces=discount_interfaces,
    )
    updated = conn.execute(
        "UPDATE recharge_packages SET name=%s, amount_fen=%s, credits=%s, discount_rate=%s, "
        "discount_interfaces=%s, sort_order=%s, is_active=%s, "
        "created_by_user_id=COALESCE(created_by_user_id, %s), version=version+1, "
        "updated_at=clock_timestamp() WHERE id=%s AND version=%s",
        (
            str(name).strip(),
            amount_fen,
            credits,
            rate,
            json.dumps(interfaces),
            int(sort_order),
            bool(is_active),
            actor_user_id,
            package_id,
            int(expected_version),
        ),
    )
    if updated.rowcount != 1:
        exists = conn.execute(
            "SELECT version FROM recharge_packages WHERE id = %s", (package_id,)
        ).fetchone()
        if exists is None:
            raise PackageNotFound(f"充值套餐不存在：{package_id}")
        raise PackageVersionConflict(
            f"充值套餐版本冲突：期望 {expected_version}，当前 {int(exists['version'])}"
        )
    record = read_package(conn, package_id)
    assert record is not None  # 刚更新的行必然可读
    return record


def grant_package_discount(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_recharge_order_id: str,
    discount_rate: object | None,
    discount_interfaces: Sequence[str] = (),
) -> str | None:
    """充值结算成功后授予套餐权益（同事务、幂等、永久有效）.

    - ``discount_rate is None``（无权益套餐）：no-op，不触碰任何已有权益。
    - 同一订单重复授予：返回既有权益行 id，不重复插入（结算重放安全）。
    - 带权益套餐最近覆盖：停用该用户此前的**套餐来源**旧行；手工折扣不受影响。
    - 同一用户的并发授予以事务级 advisory 锁串行化：“查重→停用→插入”若不串行，
      并发结算可产生两条 active 权益行（最近覆盖语义失效）。
    """
    if discount_rate is None:
        return None
    if conn.is_postgres:
        # 锁键与用户绑定：不同用户互不阻塞；事务结束（commit/rollback）自动释放。
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('billing:customer-discount:' || %s))",
            (user_id,),
        )
    rate = validate_discount_rate(discount_rate).quantize(_RATE_QUANTUM)
    interfaces = normalize_interfaces(list(discount_interfaces))
    existing = conn.execute(
        "SELECT id FROM customer_discounts WHERE source_recharge_order_id = %s",
        (source_recharge_order_id,),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    conn.execute(
        "UPDATE customer_discounts SET is_active = false, updated_at = clock_timestamp() "
        "WHERE user_id = %s AND source_recharge_order_id IS NOT NULL AND is_active",
        (user_id,),
    )
    discount_id = f"rpkg-discount-{uuid4().hex}"
    conn.execute(
        "INSERT INTO customer_discounts (id, user_id, discount_rate, priority, "
        "applicable_interfaces, is_active, source_recharge_order_id) "
        "VALUES (%s, %s, %s, %s, %s, true, %s)",
        (
            discount_id,
            user_id,
            rate,
            PACKAGE_DISCOUNT_PRIORITY,
            json.dumps(interfaces),
            source_recharge_order_id,
        ),
    )
    return discount_id


def grant_discount_from_snapshot(
    conn: BusinessConnection,
    *,
    user_id: str,
    source_recharge_order_id: str,
    package_snapshot: dict[str, object] | None,
) -> str | None:
    """结算侧入口：从订单冻结的套餐快照授予权益（无快照/无折扣 → no-op）."""
    if package_snapshot is None:
        return None
    raw_rate = package_snapshot.get("discount_rate")
    if raw_rate is None:
        return None
    raw_interfaces = package_snapshot.get("discount_interfaces")
    interfaces = [str(item) for item in raw_interfaces] if isinstance(raw_interfaces, list) else []
    return grant_package_discount(
        conn,
        user_id=user_id,
        source_recharge_order_id=source_recharge_order_id,
        discount_rate=raw_rate,
        discount_interfaces=interfaces,
    )
