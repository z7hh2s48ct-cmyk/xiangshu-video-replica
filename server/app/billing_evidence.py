"""Append verified evidence without rewriting accepted prices or cost snapshots."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException

from app.db_portable import BusinessConnection
from app.usage_billing import finish_operation


def record_evidence(
    conn: BusinessConnection,
    *,
    operation_id: str,
    actor_id: str,
    reference: str,
    reason: str,
    attempt_id: str | None = None,
    cost_fen: Decimal | None = None,
    units: Decimal | None = None,
) -> str:
    operation = conn.execute(
        "SELECT * FROM billing_operations WHERE id=%s", (operation_id,)
    ).fetchone()
    if operation is None:
        raise HTTPException(404, detail="请求不存在")
    if attempt_id is not None:
        conn.execute("SELECT id FROM billing_attempts WHERE id=%s FOR UPDATE", (attempt_id,))
        attempt = conn.execute(
            "SELECT * FROM billing_effective_attempts WHERE id=%s AND operation_id=%s",
            (attempt_id, operation_id),
        ).fetchone()
        if attempt is None or attempt["effective_cost_fen"] is not None:
            raise HTTPException(409, detail="调用不存在或成本已确认，请刷新查看")
        if operation["state"] == "PENDING":
            raise HTTPException(409, detail="请等待业务请求结束后再核对供应商成本")
    else:
        service = str(operation["service"])
        table, column = {
            "video_768p": ("generation_tasks", "actual_output_seconds"),
            "video_2k": ("generation_tasks", "actual_output_seconds"),
            "oral": ("oral_tasks", "duration_sec"),
            "asr": ("script_from_audio_tasks", "result_json"),
        }.get(service, ("", ""))
        if not table or units is None or operation["state"] != "PENDING":
            raise HTTPException(409, detail="仅可为已交付且缺少时长的待结算请求补充时长")
        if service == "oral":
            from app.internal_billing import _lock_oral_capacity_row

            _lock_oral_capacity_row(conn)
        # Match worker lock order: capacity, task, operation, wallet, funding lots.
        task = conn.execute(
            f"SELECT * FROM {table} WHERE id=%s FOR UPDATE", (operation["source_id"],)
        ).fetchone()  # noqa: S608 - fixed table map
        if task is None or task["status"] != "SUCCEEDED":
            raise HTTPException(409, detail="任务尚未成功交付，不能按成功扣费")
        value = task[column]
        if service == "asr":
            payload = json.loads(str(value or "{}"))
            if payload.get("duration_sec") is not None:
                raise HTTPException(409, detail="时长已有记录，请等待自动结算")
            payload["duration_sec"] = str(units)
            value = json.dumps(payload)
        else:
            if value is not None:
                raise HTTPException(409, detail="时长已有记录，请等待自动结算")
            value = units
        conn.execute(f"UPDATE {table} SET {column}=%s WHERE id=%s", (value, operation["source_id"]))  # noqa: S608 - fixed table and column map
        if service == "asr":
            finish_operation(conn, operation_id=operation_id, units=units, succeeded=True)
        else:
            from app.internal_billing import finalize_internal_billing, finalize_oral_billing

            if service == "oral":
                result = finalize_oral_billing(conn, oral_task_id=str(operation["source_id"]))
            else:
                result = finalize_internal_billing(
                    conn, task_id=str(operation["source_id"]), outcome="success"
                )
            if result.transaction_type is None:
                raise HTTPException(409, detail="缺少可交付结果，无法结算")
    evidence_id = str(uuid4())
    conn.execute(
        "INSERT INTO billing_evidence(id,operation_id,attempt_id,units,cost_fen,reference,"
        "reason,actor_user_id) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (evidence_id, operation_id, attempt_id, units, cost_fen, reference, reason, actor_id),
    )
    conn.execute(
        "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
        "VALUES(%s,%s,'billing.evidence.append','billing_operation',%s,%s)",
        (
            str(uuid4()),
            actor_id,
            operation_id,
            json.dumps({"evidence_id": evidence_id, "reference": reference, "reason": reason}),
        ),
    )
    return evidence_id
