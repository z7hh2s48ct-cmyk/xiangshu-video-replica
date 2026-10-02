"""Durable shared-collection billing: supplier requests once, customer revenue separately."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.billing_catalog import retail_snapshot
from app.billing_reports import date_bounds
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.sql_pagination import PAGE_CLAUSE
from app.usage_billing import accept_operation, complete_attempt, finish_operation

# Source HTTP calls use bounded timeouts. This ceiling also fences a late response
# after process loss; expiry never proves whether the supplier charged the request.
COLLECTION_REQUEST_DEADLINE_SECONDS = 600


@dataclass(frozen=True)
class FrozenCollectionBilling:
    """确认时的成员和单价只捕获一次，避免后续读扩大收费范围。"""

    user_ids: tuple[str, ...]
    pricing_json: str


def freeze_collection_billing(conn: BusinessConnection) -> FrozenCollectionBilling:
    return FrozenCollectionBilling(
        tuple(eligible_collection_users(conn)),
        json.dumps(retail_snapshot(conn, "viral_data", 1)),
    )


def reconcile_collection_requests(conn: BusinessConnection) -> int:
    rows = conn.execute(
        """SELECT id FROM billing_operations WHERE collection_batch_id IS NOT NULL
        AND user_id IS NULL AND state='PENDING'
        AND created_at < now() - (%s * interval '1 second')
        ORDER BY created_at,id LIMIT 100 FOR UPDATE SKIP LOCKED""",
        (COLLECTION_REQUEST_DEADLINE_SECONDS,),
    ).fetchall()
    for row in rows:
        for attempt in conn.execute(
            "SELECT id FROM billing_attempts WHERE operation_id=%s AND state='PENDING'", (row[0],)
        ).fetchall():
            complete_attempt(conn, attempt_id=str(attempt[0]), usage=None)
        finish_operation(conn, operation_id=str(row[0]), units=0, succeeded=False)
    return len(rows)


def eligible_collection_users(conn: BusinessConnection, *, user_id: str | None = None) -> list[str]:
    """Use the management account status, excluding suspended and unactivated codes.

    Password self-registration creates an active account without an activation code.
    Historical internal accounts without activation evidence are never charged.
    """
    return [
        str(row[0])
        for row in conn.execute(
            """SELECT u.id FROM users u LEFT JOIN LATERAL (
          SELECT c.status FROM activation_code_activations a
          JOIN activation_codes c ON c.id=a.code_id WHERE a.user_id=u.id
          ORDER BY (c.status IN ('ACTIVE','SUSPENDED')) DESC,a.activated_at DESC,a.id DESC
          LIMIT 1 FOR SHARE OF c
        ) code ON TRUE WHERE u.role='customer' AND u.is_active=1
          AND (code.status='ACTIVE' OR (code.status IS NULL
            AND u.registration_source='self_register'))
          AND (%s::text IS NULL OR u.id=%s) ORDER BY u.id FOR SHARE OF u""",
            (user_id, user_id),
        ).fetchall()
    ]


def settle_collection_charges(*, limit: int = 100) -> int:
    """Recover each confirmed request/member pair, atomically with its wallet entries.

    Missing receipts are durable work; insufficient balances are terminal receipts,
    so neither worker retries nor later top-ups silently collect old failed charges.
    """
    processed = 0
    with pg_transaction() as raw:
        reconcile_collection_requests(BusinessConnection.postgres(raw))
    for _ in range(max(0, min(limit, 1000))):
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            row = conn.execute(
                """SELECT p.id AS request_id,m.user_id,m.batch_id,b.pricing_snapshot_json
                FROM viral_collection_members m JOIN viral_collection_batches b ON b.id=m.batch_id
                JOIN billing_operations p ON p.collection_batch_id=m.batch_id
                WHERE p.user_id IS NULL AND p.state='SUCCEEDED' AND p.actual_units=1
                  AND NOT EXISTS(SELECT 1 FROM viral_collection_charges c
                    WHERE c.request_id=p.id AND c.user_id=m.user_id)
                ORDER BY p.created_at,p.id,m.user_id LIMIT 1 FOR UPDATE OF m SKIP LOCKED"""
            ).fetchone()
            if row is None:
                break
            snapshot = json.loads(row["pricing_snapshot_json"])
            operation = None
            state = "SKIPPED_INACTIVE"
            if eligible_collection_users(conn, user_id=str(row["user_id"])):
                conn.execute("SAVEPOINT collection_charge")
                try:
                    operation = accept_operation(
                        conn,
                        user_id=str(row["user_id"]),
                        service="viral_data",
                        source_id=str(row["request_id"]),
                        units=1,
                        pricing_snapshot=snapshot,
                        collection_batch_id=str(row["batch_id"]),
                    )
                    finish_operation(conn, operation_id=operation, units=1, succeeded=True)
                    state = "SUCCEEDED"
                except HTTPException as exc:
                    conn.execute("ROLLBACK TO SAVEPOINT collection_charge")
                    if exc.status_code != 402:
                        raise
                    operation, state = None, "INSUFFICIENT_CREDITS"
                finally:
                    conn.execute("RELEASE SAVEPOINT collection_charge")
            conn.execute(
                "INSERT INTO viral_collection_charges(request_id,user_id,operation_id,state,"
                "due_credits) VALUES(%s,%s,%s,%s,%s)",
                (row["request_id"], row["user_id"], operation, state, int(snapshot["credits"])),
            )
        processed += 1
    return processed


def create_collection_batch(
    conn: BusinessConnection,
    *,
    platform: str,
    config: dict[str, Any],
    user_ids: Iterable[str],
    pricing_json: str | None = None,
    stats_complete: bool = True,
) -> str:
    """Freeze recipients and one-request retail price before any source call."""
    batch_id = str(uuid4())
    conn.execute(
        "INSERT INTO viral_collection_batches"
        "(id,platform,config_json,pricing_snapshot_json,run_status,stats_version) "
        "VALUES (%s,%s,%s,%s,'PENDING',%s)",
        (
            batch_id,
            platform,
            json.dumps(config, ensure_ascii=False),
            pricing_json
            if pricing_json is not None
            else json.dumps(retail_snapshot(conn, "viral_data", 1)),
            1 if stats_complete else None,
        ),
    )
    for user_id in dict.fromkeys(user_ids):
        conn.execute(
            "INSERT INTO viral_collection_members(batch_id,user_id) VALUES (%s,%s)",
            (batch_id, user_id),
        )
    return batch_id


def collection_batch_rows(
    conn: BusinessConnection, *, start: date, end: date, limit: int = 25, offset: int = 0
) -> list[dict[str, Any]]:
    lower, upper = date_bounds(start, end)
    rows = conn.execute(
        f"""WITH requests AS (
          SELECT collection_batch_id AS batch_id,count(*) AS request_count,
            count(*) FILTER(WHERE state='SUCCEEDED' AND actual_units=1) AS confirmed_count,
            count(*) FILTER(WHERE state='FAILED') AS uncertain_count,
            count(*) FILTER(WHERE state='PENDING') AS pending_requests
          FROM billing_operations WHERE collection_batch_id IS NOT NULL AND user_id IS NULL
          GROUP BY collection_batch_id
        ), costs AS (
          SELECT o.collection_batch_id AS batch_id,sum(a.effective_cost_fen) AS known_cost_fen,
            count(*) FILTER(WHERE a.effective_cost_fen IS NULL) AS unknown_cost_count
          FROM billing_effective_attempts a JOIN billing_operations o ON o.id=a.operation_id
          WHERE o.collection_batch_id IS NOT NULL AND o.user_id IS NULL
          GROUP BY o.collection_batch_id
        ), members AS (
          SELECT batch_id,count(*) AS customer_count FROM viral_collection_members GROUP BY batch_id
        ), charges AS (
          SELECT p.collection_batch_id AS batch_id,count(*) AS processed_count,
            count(*) FILTER(WHERE c.state='INSUFFICIENT_CREDITS') AS failed_count,
            count(*) FILTER(WHERE c.state='SKIPPED_INACTIVE') AS skipped_count,
            sum(o.charged_credits) AS charged_credits,sum(o.revenue_fen) AS known_revenue_fen,
            count(*) FILTER(WHERE o.id IS NOT NULL AND o.revenue_fen IS NULL) AS
              unknown_revenue_count
          FROM viral_collection_charges c JOIN billing_operations p ON p.id=c.request_id
          LEFT JOIN billing_operations o ON o.id=c.operation_id GROUP BY p.collection_batch_id
        ) SELECT b.*,COALESCE(r.request_count,0) AS request_count,
          COALESCE(r.confirmed_count,0) AS confirmed_count,
          COALESCE(r.uncertain_count,0) AS uncertain_count,
          COALESCE(r.pending_requests,0) AS pending_requests,
          COALESCE(m.customer_count,0) AS customer_count,
          COALESCE(r.confirmed_count,0)*COALESCE(m.customer_count,0)-COALESCE(c.processed_count,0)
            AS pending_charges,
          COALESCE(c.failed_count,0) AS failed_count,
          COALESCE(c.skipped_count,0) AS skipped_count,
          COALESCE(c.charged_credits,0) AS charged_credits,
          COALESCE(c.known_revenue_fen,0) AS known_revenue_fen,
          COALESCE(c.unknown_revenue_count,0) AS unknown_revenue_count,
          COALESCE(k.known_cost_fen,0) AS known_cost_fen,
          COALESCE(k.unknown_cost_count,0) AS unknown_cost_count,count(*) OVER() AS total_count
        FROM viral_collection_batches b LEFT JOIN requests r ON r.batch_id=b.id
        LEFT JOIN members m ON m.batch_id=b.id LEFT JOIN charges c ON c.batch_id=b.id
        LEFT JOIN costs k ON k.batch_id=b.id WHERE b.created_at>=%s AND b.created_at<%s
        ORDER BY b.created_at DESC,b.id DESC {PAGE_CLAUSE}""",
        (lower, upper, limit, offset),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["config"] = json.loads(item.pop("config_json"))
        item["pricing"] = json.loads(item.pop("pricing_snapshot_json"))
        item["profit_fen"] = (
            None
            if any(
                item[key]
                for key in (
                    "pending_requests",
                    "pending_charges",
                    "unknown_cost_count",
                    "unknown_revenue_count",
                )
            )
            else item["known_revenue_fen"] - item["known_cost_fen"]
        )
        result.append(item)
    return result


def collection_charge_rows(
    conn: BusinessConnection, *, batch_id: str, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""SELECT c.*,u.username,COALESCE(o.charged_credits,0) AS charged_credits,
            o.revenue_fen,count(*) OVER() AS total_count
        FROM viral_collection_charges c JOIN billing_operations p ON p.id=c.request_id
        JOIN users u ON u.id=c.user_id LEFT JOIN billing_operations o ON o.id=c.operation_id
        WHERE p.collection_batch_id=%s ORDER BY c.created_at DESC,c.request_id,c.user_id
        {PAGE_CLAUSE}""",
            (batch_id, limit, offset),
        ).fetchall()
    ]
