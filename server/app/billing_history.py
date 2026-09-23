"""BILLING-OBS P2-3 价目历史版本：审计为源重建版本序列，当前行兜底。

历史账单上的「费率 V{n}」要能事后回查，但 ``billing_tariffs`` 只保留当前
一行——版本序列只能从 ``audit_logs`` 重建：``billing.tariff.update`` 的
``metadata_json`` 里存着每一次调整的 old/new 快照。本模块在 Python 侧解析
JSON（不用 jsonb 运算符），因此读逻辑不绑定具体 SQL 方言；只有确实没有
审计的当前行（历史遗留或旧版写入路径）才以 ``source='current'`` 兜底。

用户端只回公开价目（unit_credits / unit_rounding），供应商成本、操作人和
调整原因只出现在管理端响应里。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.billing_catalog import SERVICES
from app.db_portable import BusinessConnection

TARIFF_ACTION = "billing.tariff.update"
PRICING_ACTION = "customer_pricing.update"
MAX_HISTORY_LIMIT = 100


def _format_timestamp(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _metadata(value: object) -> dict[str, Any]:
    """Parse one audit metadata payload; malformed rows degrade to ``{}``."""
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _version_of(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _tariff_entry(
    version: int,
    body: dict[str, Any],
    *,
    source: str,
    actor_user_id: object = None,
    actor_username: object = None,
    reason: object = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    return {
        "version": version,
        "enabled": bool(body.get("enabled")),
        "unit_credits": _text(body.get("unit_credits")),
        "unit_cost_fen": _text(body.get("unit_cost_fen")),
        "unit_rounding": str(body.get("unit_rounding") or "ceil"),
        "source": source,
        "current": False,
        "actor_user_id": str(actor_user_id) if actor_user_id else None,
        "actor_username": str(actor_username) if actor_username else None,
        "reason": str(reason) if reason else None,
        "effective_at": effective_at,
    }


def _audit_rows(
    conn: BusinessConnection, action: str, entity_type: str, entity_id: str | None = None
) -> list[Any]:
    where = "al.action = %s AND al.entity_type = %s"
    params: list[object] = [action, entity_type]
    if entity_id is not None:
        where += " AND al.entity_id = %s"
        params.append(entity_id)
    return conn.execute(
        "SELECT al.actor_user_id, COALESCE(u.username, ''), al.metadata_json, al.created_at "
        "FROM audit_logs al LEFT JOIN users u ON u.id = al.actor_user_id "
        f"WHERE {where} ORDER BY al.created_at DESC, al.id DESC",
        tuple(params),
    ).fetchall()


def _trim(entries: dict[int, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, MAX_HISTORY_LIMIT))
    return [entries[version] for version in sorted(entries, reverse=True)][:bounded]


def _current_tariff(
    conn: BusinessConnection, service: str
) -> tuple[int, dict[str, Any], str | None] | None:
    row = conn.execute(
        "SELECT enabled, unit_credits, unit_cost_fen, unit_rounding, version, updated_at "
        "FROM billing_tariffs WHERE service = %s",
        (service,),
    ).fetchone()
    if row is None:
        return None
    return (
        int(row[4]),
        {
            "enabled": row[0],
            "unit_credits": row[1],
            "unit_cost_fen": row[2],
            "unit_rounding": row[3],
        },
        _format_timestamp(row[5]),
    )


def tariff_versions(conn: BusinessConnection, service: str, limit: int = 20) -> dict[str, Any]:
    """One subject's price-version sequence, newest first (admin view)."""
    if service not in SERVICES:
        raise ValueError("未知计费科目")
    entries: dict[int, dict[str, Any]] = {}
    for row in _audit_rows(conn, TARIFF_ACTION, "billing_tariff", service):
        metadata = _metadata(row[2])
        body = _metadata(metadata.get("new"))
        version = _version_of(body.get("version"))
        if version <= 0 or version in entries:
            continue
        entries[version] = _tariff_entry(
            version,
            body,
            source="audit",
            actor_user_id=row[0],
            actor_username=row[1],
            reason=metadata.get("reason"),
            effective_at=_format_timestamp(row[3]),
        )
    current = _current_tariff(conn, service)
    current_version = current[0] if current is not None else 0
    if current is not None and current_version not in entries:
        entries[current_version] = _tariff_entry(
            current_version,
            current[1],
            source="current",
            effective_at=current[2],
        )
    if current_version in entries:
        entries[current_version]["current"] = True
    return {
        "service": service,
        "name": SERVICES[service].name,
        "unit": SERVICES[service].unit,
        "current_version": current_version,
        "items": _trim(entries, limit),
    }


def pricing_versions(conn: BusinessConnection, limit: int = 20) -> dict[str, Any]:
    """The recharge-conversion config sequence, newest first (admin view)."""
    entries: dict[int, dict[str, Any]] = {}
    for row in _audit_rows(conn, PRICING_ACTION, "customer_pricing"):
        metadata = _metadata(row[2])
        version = _version_of(metadata.get("version"))
        if version <= 0 or version in entries:
            continue
        config = metadata.get("new")
        entries[version] = {
            "version": version,
            "config": config if isinstance(config, dict) else None,
            "source": "audit",
            "current": False,
            "actor_user_id": str(row[0]) if row[0] else None,
            "actor_username": str(row[1]) if row[1] else None,
            "reason": str(metadata["reason"]) if metadata.get("reason") else None,
            "effective_at": _format_timestamp(row[3]),
        }
    current = conn.execute(
        "SELECT version, config_json, updated_at FROM customer_credit_pricing WHERE id = 1"
    ).fetchone()
    current_version = int(current[0]) if current is not None else 0
    if current is not None and current_version not in entries:
        try:
            config = json.loads(str(current[1])) if current[1] else None
        except ValueError:
            config = None
        entries[current_version] = {
            "version": current_version,
            "config": config if isinstance(config, dict) else None,
            "source": "current",
            "current": False,
            "actor_user_id": None,
            "actor_username": None,
            "reason": None,
            "effective_at": _format_timestamp(current[2]),
        }
    if current_version in entries:
        entries[current_version]["current"] = True
    return {"current_version": current_version, "items": _trim(entries, limit)}


def retail_version(conn: BusinessConnection, service: str, version: int) -> dict[str, Any]:
    """What the public price was at ``version`` — customer-safe fields only."""
    if service not in SERVICES:
        raise ValueError("未知计费科目")
    if not SERVICES[service].customer_charge_allowed:
        raise ValueError("该科目不对用户计费")
    if version < 0:
        raise ValueError("版本号必须为非负整数")
    history = tariff_versions(conn, service, limit=MAX_HISTORY_LIMIT)
    entry = next((item for item in history["items"] if item["version"] == version), None)
    return {
        "service": service,
        "name": SERVICES[service].name,
        "unit": SERVICES[service].unit,
        "version": version,
        "current_version": history["current_version"],
        "found": entry is not None,
        "current": bool(entry and entry["current"]),
        "enabled": entry["enabled"] if entry else None,
        "unit_credits": entry["unit_credits"] if entry else None,
        "unit_rounding": entry["unit_rounding"] if entry else None,
        "effective_at": entry["effective_at"] if entry else None,
    }
