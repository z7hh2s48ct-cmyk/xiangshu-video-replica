"""持久记录实际素材事件；没有账单证据时保留未知金额，不套假费率。"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from app.admin_dates import utc_timestamp_sql
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

logger = logging.getLogger(__name__)
KINDS = {
    "download": ("素材下载", "byte"),
    "upload": ("成功提交转存字节", "byte"),
    "storage_snapshot": ("已存素材大小", "byte"),
    "storage_retention": ("应用观察存储保留量", "byte_millisecond"),
    "backend_read": ("后端实际读取", "byte"),
    "preparation": ("素材准备耗时", "millisecond"),
    "download_intent": ("签发播放下载地址", "intent"),
}


def object_identity(namespace: str, key: str) -> str:
    # 只存不可逆标识，不将签名地址、存储凭据或任意源站链接写入业务证据。
    return hashlib.sha256(f"{namespace}|{key}".encode()).hexdigest()


def _append(
    conn: BusinessConnection, ident: str, action: str, entity: str, data: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
        "VALUES(%s,NULL,%s,'viral_resource',%s,%s)",
        (ident, action, entity, json.dumps(data, ensure_ascii=False, sort_keys=True)),
    )


class ResourceMeasurement:
    def __init__(
        self,
        *,
        enabled: bool,
        platform: str,
        video_id: str,
        kind: str,
        object_hash: str | None = None,
    ) -> None:
        self.id: str | None = None
        self.started = time.monotonic()
        self.quantity: int | None = None
        self.closed = False
        self.data = {
            "schema": "viral-resource-v1",
            "platform": platform,
            "video_id": video_id,
            "kind": kind,
            "unit": KINDS[kind][1],
            "object_hash": object_hash,
            "rate_fen": None,
            "cost_fen": None,
        }
        if enabled:
            try:
                with pg_transaction() as raw:
                    self.id = str(uuid4())
                    _append(
                        BusinessConnection.postgres(raw),
                        self.id,
                        "viral_resource.started",
                        self.id,
                        self.data,
                    )
            except Exception:
                self.id = None
                logger.warning("素材计量起点未持久化；本次成本证据不完整。")

    def finish(self, state: str) -> None:
        if self.closed:
            return
        self.closed = True
        if self.id is None:
            return
        try:
            with pg_transaction() as raw:
                _append(
                    BusinessConnection.postgres(raw),
                    str(uuid4()),
                    "viral_resource.finished",
                    self.id,
                    {
                        **self.data,
                        "measurement_id": self.id,
                        "state": state,
                        "quantity": self.quantity,
                        "elapsed_ms": max(0, round((time.monotonic() - self.started) * 1000)),
                    },
                )
        except Exception:
            logger.warning("素材计量终点未持久化；本次证据保持进行中或未知。")


def measured_chunks(source: Iterator[bytes], measurement: ResourceMeasurement) -> Iterator[bytes]:
    measurement.quantity = 0
    state = "ABORTED"
    try:
        for chunk in source:
            measurement.quantity += len(chunk)
            yield chunk
        state = "SUCCEEDED"
    except Exception:
        state = "FAILED"
        raise
    finally:
        try:
            close = getattr(source, "close", None)
            if close:
                close()
        finally:
            measurement.finish(state)


def stored_resource(
    *, enabled: bool, platform: str, video_id: str, namespace: str, key: str, size: int
) -> None:
    measurement = ResourceMeasurement(
        enabled=enabled,
        platform=platform,
        video_id=video_id,
        kind="storage_snapshot",
        object_hash=object_identity(namespace, key),
    )
    measurement.quantity = size
    measurement.finish("SUCCEEDED")
    if measurement.id is not None:
        try:
            with pg_transaction() as raw:
                raw.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (measurement.data["object_hash"],)
                )
                opened = raw.execute(
                    "SELECT id FROM audit_logs s WHERE action='viral_resource.started' "
                    "AND metadata_json::jsonb->>'kind'='storage_retention' "
                    "AND metadata_json::jsonb->>'object_hash'=%s AND NOT EXISTS "
                    "(SELECT 1 FROM audit_logs f WHERE f.action='viral_resource.finished' "
                    "AND f.entity_id=s.id)",
                    (measurement.data["object_hash"],),
                ).fetchone()
                if opened is None:
                    ident = str(uuid4())
                    _append(
                        BusinessConnection.postgres(raw),
                        ident,
                        "viral_resource.started",
                        ident,
                        {
                            **measurement.data,
                            "kind": "storage_retention",
                            "unit": "byte_millisecond",
                            "object_bytes": size,
                        },
                    )
        except Exception:
            logger.warning("存储保留起点未记录；存储成本仍未知。")


def deleted_resource(namespace: str, key: str) -> None:
    if "viral/" not in key:
        return
    try:
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            identity = object_identity(namespace, key)
            raw.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (identity,))
            rows = conn.execute(
                "SELECT s.id,s.metadata_json,"
                "GREATEST(0,floor(extract(epoch FROM (clock_timestamp()-"
                f"{utc_timestamp_sql('s.created_at')}))*1000)) AS elapsed_ms "
                "FROM audit_logs s WHERE s.action='viral_resource.started' "
                "AND s.metadata_json::jsonb->>'kind'='storage_retention' "
                "AND s.metadata_json::jsonb->>'object_hash'=%s AND NOT EXISTS "
                "(SELECT 1 FROM audit_logs f WHERE f.action='viral_resource.finished' "
                "AND f.entity_id=s.id)",
                (identity,),
            ).fetchall()
            for row in rows:
                data = json.loads(str(row["metadata_json"]))
                elapsed = int(row["elapsed_ms"])
                _append(
                    conn,
                    str(uuid4()),
                    "viral_resource.finished",
                    str(row["id"]),
                    {
                        **data,
                        "measurement_id": str(row["id"]),
                        "state": "SUCCEEDED",
                        "quantity": int(data["object_bytes"]) * elapsed,
                        "elapsed_ms": elapsed,
                    },
                )
    except Exception:
        logger.warning("存储删除终点未记录；保留时段仍待核对。")


def measured_object_chunks(source: Iterator[bytes], *, namespace: str, key: str) -> Iterator[bytes]:
    if "viral/" not in key:
        return source
    try:
        with pg_transaction() as raw:
            row = raw.execute(
                "SELECT metadata_json FROM audit_logs "
                "WHERE action='viral_resource.started' AND "
                "metadata_json::jsonb->>'kind'='storage_snapshot' AND "
                "metadata_json::jsonb->>'object_hash'=%s ORDER BY created_at DESC,id DESC LIMIT 1",
                (object_identity(namespace, key),),
            ).fetchone()
        if row:
            data = json.loads(str(row[0]))
            measurement = ResourceMeasurement(
                enabled=True,
                platform=data["platform"],
                video_id=data["video_id"],
                kind="backend_read",
            )
            return measured_chunks(source, measurement)
    except Exception:
        logger.warning("素材读取缺计量关联；不推测流量成本。")
    return source


def resource_records(
    conn: BusinessConnection,
    *,
    platform: str | None = None,
    video_id: str | None = None,
    lower: object = None,
    upper: object = None,
) -> list[dict[str, Any]]:
    filters = "s.action='viral_resource.started' AND s.entity_type='viral_resource'"
    params: list[object] = []
    for key, value in (("platform", platform), ("video_id", video_id)):
        if value is not None:
            filters += f" AND s.metadata_json::jsonb->>'{key}'=%s"
            params.append(value)
    if lower is not None:
        timestamp = utc_timestamp_sql("s.created_at")
        filters += f" AND {timestamp} >= %s AND {timestamp} < %s"
        params.extend([lower, upper])
    rows = conn.execute(
        "SELECT s.id,s.created_at,s.metadata_json,"
        "f.metadata_json AS finished,c.metadata_json AS cost_evidence "
        "FROM audit_logs s LEFT JOIN LATERAL (SELECT metadata_json FROM audit_logs "
        "WHERE action='viral_resource.finished' AND entity_id=s.id "
        "ORDER BY created_at DESC,id DESC LIMIT 1) f ON TRUE "
        "LEFT JOIN LATERAL (SELECT metadata_json FROM audit_logs "
        "WHERE action='viral_resource.cost_verified' AND entity_id=s.id "
        "ORDER BY created_at DESC,id DESC LIMIT 1) c ON TRUE "
        f"WHERE {filters} ORDER BY s.created_at DESC,s.id",
        tuple(params),
    ).fetchall()
    result = []
    for row in rows:
        data = json.loads(str(row["metadata_json"]))
        final = json.loads(str(row["finished"])) if row["finished"] else {}
        evidence = json.loads(str(row["cost_evidence"])) if row["cost_evidence"] else None
        result.append(
            {
                **data,
                "id": str(row["id"]),
                "label": KINDS[data["kind"]][0],
                "created_at": str(row["created_at"]),
                "state": final.get("state", "PENDING"),
                "quantity": (
                    str(final["quantity"])
                    if data["kind"] == "storage_retention" and final.get("quantity") is not None
                    else final.get("quantity")
                ),
                "elapsed_ms": final.get("elapsed_ms"),
                "cost_fen": float(evidence["cost_fen"]) if evidence else None,
                "evidence": evidence,
            }
        )
    return result


def resource_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    components = []
    for kind, (label, unit) in KINDS.items():
        subset = [row for row in records if row["kind"] == kind]
        components.append(
            {
                "kind": kind,
                "label": label,
                "unit": unit,
                "events": len(subset),
                "knownQuantity": str(
                    sum(int(row["quantity"]) for row in subset if row["quantity"] is not None)
                ),
                "unknownQuantityEvents": sum(row["quantity"] is None for row in subset),
                "knownCostFen": sum(
                    row["cost_fen"] for row in subset if row["cost_fen"] is not None
                ),
                "unknownCostEvents": sum(row["cost_fen"] is None for row in subset),
                "costFen": None
                if not subset or any(row["cost_fen"] is None for row in subset)
                else sum(row["cost_fen"] for row in subset),
            }
        )
    return {
        "components": components,
        "records": records,
        "coverageComplete": False,
        "knownCostFen": sum(row["cost_fen"] for row in records if row["cost_fen"] is not None),
        "costFen": None,
        "missingEvidence": [
            "历史未观测事件无法还原",
            "云端直链实际流量须供应商日志或账单",
            "存储保留时段和供应商计费规则待核对",
            "下载重定向及存储SDK重试计费量待账单核对",
        ],
        "countingRule": (
            "记录实际观察事件与字节，准备耗时是墙钟耗时；对象大小不是存储字节时长，"
            "签发地址不是实际下载。确认成本仅来自指定事件的账单证据，"
            "不分摊共享搜索，不补默认费率。"
        ),
    }
