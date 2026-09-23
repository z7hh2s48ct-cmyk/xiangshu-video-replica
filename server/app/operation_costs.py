from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from app.db_portable import BusinessConnection


@dataclass(frozen=True)
class GenerationRateSnapshot:
    cost_subject: str
    cost_unit_price_fen: Decimal | None
    external_unit_price_fen: int | None
    billed_seconds: int


def _resolution_suffix(resolution: str) -> str:
    normalized = resolution.strip().lower()
    if normalized not in {"768p", "2k"}:
        raise ValueError(f"unsupported rate resolution: {resolution}")
    return normalized


def _rate(conn: BusinessConnection, subject: str) -> tuple[str, Decimal | None]:
    from app.billing_catalog import COST_SUBJECTS, SERVICES, read_tariff

    service = COST_SUBJECTS.get(subject, subject)
    if service not in SERVICES:
        return "call", None
    tariff = read_tariff(conn, service)
    return SERVICES[service].unit, tariff.unit_cost_fen if tariff else None


def snapshot_generation_rates(
    conn: BusinessConnection,
    *,
    task_id: str,
    resolution: str,
    billed_seconds: int,
) -> GenerationRateSnapshot:
    """Freeze customer and upstream rates in the task creation transaction."""
    if billed_seconds < 1:
        raise ValueError("billed_seconds must be positive")
    suffix = _resolution_suffix(resolution)
    cost_subject = f"video_generation_{suffix}"
    if not getattr(conn, "is_postgres", False):
        return GenerationRateSnapshot(cost_subject, None, None, billed_seconds)
    from app.billing_catalog import retail_snapshot

    quote = retail_snapshot(conn, "video_2k" if suffix == "2k" else "video_768p", 1)
    _, cost_price = _rate(conn, cost_subject)
    ratio = quote["points_per_yuan"]
    external_price = int(Decimal(str(quote["credits"])) * 100 / Decimal(str(ratio))) if ratio else 0
    updated = conn.execute(
        """
        UPDATE generation_tasks
        SET cost_rate_subject_snapshot = %s,
            cost_unit_price_fen_snapshot = %s,
            external_unit_price_fen_snapshot = %s,
            billed_seconds = %s,
            cost_status = 'PENDING'
        WHERE id = %s
        """,
        (
            cost_subject,
            int(cost_price) if cost_price is not None else None,
            external_price,
            billed_seconds,
            task_id,
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError("generation task disappeared before rate snapshot")
    return GenerationRateSnapshot(cost_subject, cost_price, external_price, billed_seconds)


def begin_operation_cost(
    conn: BusinessConnection,
    *,
    source_type: str,
    source_id: str,
    subject: str,
    user_id: str | None = None,
    generation_task_id: str | None = None,
    resolution: str | None = None,
    unit_price_fen: Decimal | int | None = None,
    metadata: dict[str, object] | None = None,
) -> str:
    """Create the idempotent rate snapshot immediately before a provider call."""
    if not getattr(conn, "is_postgres", False):
        return ""
    unit, configured_price = _rate(conn, subject)
    price = configured_price if unit_price_fen is None else unit_price_fen
    record_id = str(uuid4())
    row = conn.execute(
        """
        INSERT INTO operation_cost_records (
            id, source_type, source_id, subject, user_id, generation_task_id,
            resolution, unit, unit_price_fen, metadata_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_type, source_id, subject) DO UPDATE SET
            source_id = excluded.source_id
        RETURNING id
        """,
        (
            record_id,
            source_type,
            source_id,
            subject,
            user_id,
            generation_task_id,
            resolution,
            unit,
            price,
            json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True),
        ),
    ).fetchone()
    assert row is not None
    _link_billing_attempt(conn, str(row["id"]))
    return str(row["id"])


def _link_billing_attempt(conn: BusinessConnection, record_id: str) -> None:
    from app.billing_catalog import COST_SUBJECTS, SERVICES
    from app.usage_billing import find_operation

    row = conn.execute(
        "SELECT * FROM operation_cost_records WHERE id=%s FOR UPDATE", (record_id,)
    ).fetchone()
    if row is None or row["billing_attempt_id"]:
        return
    service = COST_SUBJECTS.get(str(row["subject"]), str(row["subject"]))
    if service not in SERVICES:
        return
    source = str(row["generation_task_id"] or row["source_id"]).split(":")[0]
    operation = find_operation(conn, source)
    if operation is None:
        return
    attempt = conn.execute(
        "INSERT INTO billing_attempts(id,operation_id,attempt_key,service,provider,unit,"
        "unit_cost_fen) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_id,service,attempt_key) "
        "DO UPDATE SET attempt_key=excluded.attempt_key RETURNING id",
        (
            str(uuid4()),
            operation,
            str(row["source_id"]),
            service,
            SERVICES[service].provider,
            SERVICES[service].unit,
            row["unit_price_fen"],
        ),
    ).fetchone()
    conn.execute(
        "UPDATE operation_cost_records SET billing_attempt_id=%s WHERE id=%s",
        (attempt["id"], record_id),
    )


def complete_operation_cost(
    conn: BusinessConnection,
    *,
    record_id: str,
    usage_amount: float | int | None,
) -> None:
    """Complete a provider cost record; absent usage stays visibly UNKNOWN."""
    if not getattr(conn, "is_postgres", False) or not record_id:
        return
    if usage_amount is not None and usage_amount < 0:
        raise ValueError("usage_amount must be non-negative")
    from app.billing_catalog import amount

    usage = None if usage_amount is None else amount(usage_amount)
    rate = conn.execute(
        "SELECT unit_price_fen FROM operation_cost_records WHERE id=%s", (record_id,)
    ).fetchone()
    cost = (
        Decimal(0)
        if usage == 0
        else usage * Decimal(str(rate[0]))
        if usage is not None and rate is not None and rate[0] is not None
        else None
    )
    status = "ACTUAL" if cost is not None else "UNKNOWN"
    updated = conn.execute(
        "UPDATE operation_cost_records SET usage_amount=%s,cost_fen=%s,status=%s,"
        "completed_at=now() "
        "WHERE id=%s AND (status='PENDING' OR (status=%s AND usage_amount IS NOT DISTINCT "
        "FROM %s::numeric))",
        (usage, cost, status, record_id, status, usage),
    )
    if updated.rowcount != 1:
        existing = conn.execute(
            "SELECT status, usage_amount FROM operation_cost_records WHERE id = %s", (record_id,)
        ).fetchone()
        if existing is None:
            raise RuntimeError("operation cost record disappeared")
        raise RuntimeError(
            "operation cost record was already completed with a different result: "
            f"status={existing['status']}, usage_amount={existing['usage_amount']}"
        )

    _link_billing_attempt(conn, record_id)
    linked = conn.execute(
        "SELECT billing_attempt_id FROM operation_cost_records WHERE id=%s", (record_id,)
    ).fetchone()
    if linked and linked[0]:
        from app.usage_billing import complete_attempt

        complete_attempt(conn, attempt_id=str(linked[0]), usage=usage)


def record_video_generation_cost(
    conn: BusinessConnection, *, task_id: str, output_seconds: float | None
) -> None:
    row = conn.execute(
        "SELECT cost_rate_subject_snapshot FROM generation_tasks WHERE id=%s", (task_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("generation task does not exist")
    record = conn.execute(
        "SELECT id FROM operation_cost_records WHERE generation_task_id=%s "
        "ORDER BY occurred_at DESC,id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if record is None:
        conn.execute(
            "UPDATE generation_tasks SET actual_output_seconds=%s,cost_status='UNKNOWN' "
            "WHERE id=%s",
            (output_seconds, task_id),
        )
        return
    record_id = str(record[0])
    complete_operation_cost(conn, record_id=record_id, usage_amount=output_seconds)
    cost = conn.execute(
        "SELECT cost_fen,status FROM operation_cost_records WHERE id=%s", (record_id,)
    ).fetchone()
    conn.execute(
        "UPDATE generation_tasks SET actual_output_seconds=%s,actual_cost=%s,"
        "cost_status=%s WHERE id=%s",
        (
            output_seconds,
            Decimal(str(cost[0])) / 100 if cost[0] is not None else None,
            cost[1],
            task_id,
        ),
    )


def record_video_generation_not_called(
    conn: BusinessConnection,
    *,
    task_id: str,
) -> None:
    """Close submission snapshots at zero when no provider call was made."""
    if not getattr(conn, "is_postgres", False):
        return
    records = conn.execute(
        """
        SELECT id FROM operation_cost_records
        WHERE source_type = 'generation_task' AND generation_task_id = %s AND status = 'PENDING'
        """,
        (task_id,),
    ).fetchall()
    for record in records:
        complete_operation_cost(conn, record_id=str(record["id"]), usage_amount=0)
    updated = conn.execute(
        """
        UPDATE generation_tasks
        SET actual_output_seconds = 0,
            actual_cost = 0,
            cost_status = 'ACTUAL'
        WHERE id = %s
        """,
        (task_id,),
    )
    if updated.rowcount != 1:
        raise RuntimeError("generation task does not exist")
