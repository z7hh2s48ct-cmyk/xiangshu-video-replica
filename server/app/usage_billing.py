"""One operation per delivered feature; shared wallet, frozen rates and funding."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.billing_catalog import (
    SERVICES,
    amount,
    credits_from_snapshot,
    read_tariff,
    retail_snapshot,
    snapshot_discount_rate,
)
from app.db_portable import BusinessConnection
from app.sub_account_permissions import enforce_sub_account_feature
from app.sub_account_quota import enforce_sub_account_quota


def resolve_wallet_owner(conn: BusinessConnection, user_id: str) -> str:
    """The wallet that pays for this operator's work (T2.10).

    A sub-account owns no wallet: its consumption settles on the master
    account's wallet while ``billing_operations.user_id`` keeps naming the
    *actor* that submitted the work. Masters, and rows with no parent —
    including a vanished row — resolve to themselves.
    """
    row = conn.execute("SELECT parent_user_id FROM users WHERE id = %s", (user_id,)).fetchone()
    if row is not None and row[0] is not None:
        return str(row[0])
    return user_id


def accept_operation(
    conn: BusinessConnection,
    *,
    user_id: str,
    service: str,
    source_id: str,
    units: Decimal | str | int | float,
    billing_round: int = 1,
    submission_id: str | None = None,
    request_fingerprint: str = "",
    pricing_snapshot: dict[str, Any] | None = None,
    collection_batch_id: str | None = None,
) -> str:
    """Called in the task-creation transaction, before any paid upstream submission.

    ``user_id`` is the *actor* (the account that submitted the work). The
    wallet, its lots and the ledger's wallet side belong to the master when
    the actor is a sub-account (T2.10); the operation row keeps the actor.
    """
    # A user-scoped lock also serializes free requests without a wallet row.
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("billing:user:" + user_id,))
    old = conn.execute(
        "SELECT id, budget_units, request_fingerprint FROM billing_operations WHERE "
        "user_id=%s AND service=%s "
        "AND source_id=%s AND billing_round=%s",
        (user_id, service, source_id, billing_round),
    ).fetchone()
    if old:
        if (
            Decimal(str(old["budget_units"])) != amount(units)
            or old["request_fingerprint"] != request_fingerprint
        ):
            raise HTTPException(
                409,
                detail={"code": "BILLING_REQUEST_CONFLICT", "message": "计费用量与原请求不一致。"},
            )
        return str(old["id"])
    # The wallet owner is resolved before pricing so the customer's package
    # discount is read from the wallet owner (a sub-account spends the
    # master's wallet and rides the master's discounts, T2.10).
    wallet_owner_id = resolve_wallet_owner(conn, user_id)
    # Feature admission (Phase 3b): restricted sub-accounts cannot use the
    # features their master disabled. Enforced before pricing and
    # independently of `credits`, so free operations are covered too.
    enforce_sub_account_feature(conn, actor_id=user_id, service=service)
    snapshot = (
        dict(pricing_snapshot)
        if pricing_snapshot is not None
        else retail_snapshot(conn, service, units, user_id=wallet_owner_id)
    )
    if snapshot["service"] != service or snapshot["unit"] != SERVICES[service].unit:
        raise ValueError("计价快照科目不匹配")
    snapshot.update(units=str(amount(units)), credits=credits_from_snapshot(snapshot, units))
    if SERVICES[service].unit == "second" and amount(units) == 0 and snapshot["enabled"]:
        raise HTTPException(
            422,
            detail={
                "code": "BILLABLE_DURATION_REQUIRED",
                "message": "缺少有效媒体时长，请重新上传后再提交。",
            },
        )
    credits = int(str(snapshot["credits"]))
    funding: list[dict[str, Any]] = []
    if credits:
        if wallet_owner_id != user_id:
            # Sub-account monthly cap (Phase 3a): enforced under the actor's
            # advisory lock, before the shared wallet is touched.
            enforce_sub_account_quota(conn, actor_id=user_id, additional_credits=credits)
        wallet = conn.execute(
            "SELECT available_credits FROM wallets WHERE user_id=%s FOR UPDATE",
            (wallet_owner_id,),
        ).fetchone()
        updated = conn.execute(
            "UPDATE wallets SET available_credits=available_credits-%s, "
            "reserved_credits=reserved_credits+%s, "
            "updated_at=now() WHERE user_id=%s AND available_credits >= %s",
            (credits, credits, wallet_owner_id, credits),
        )
        if updated.rowcount != 1:
            raise HTTPException(
                402,
                detail={
                    "code": "INSUFFICIENT_CREDITS",
                    "message": f"积分不足，本次需要 {credits} 积分。",
                },
            )
        remaining = credits
        lots = conn.execute(
            "SELECT id,credits,remaining_credits,amount_fen FROM billing_credit_lots "
            "WHERE user_id=%s AND remaining_credits>0 ORDER BY created_at,id FOR UPDATE",
            (wallet_owner_id,),
        ).fetchall()
        untracked = (
            max(0, int(wallet[0]) - sum(int(lot["remaining_credits"]) for lot in lots))
            if wallet
            else 0
        )
        unknown_used = min(remaining, untracked)
        if unknown_used:
            funding.append(
                {
                    "lot_id": None,
                    "credits": unknown_used,
                    "lot_credits": unknown_used,
                    "amount_fen": None,
                }
            )
            remaining -= unknown_used
        for lot in lots:
            if not remaining:
                break
            used = min(remaining, int(lot["remaining_credits"]))
            funding.append(
                {
                    "lot_id": str(lot["id"]),
                    "credits": used,
                    "lot_credits": int(lot["credits"]),
                    "amount_fen": str(lot["amount_fen"]) if lot["amount_fen"] is not None else None,
                }
            )
            conn.execute(
                "UPDATE billing_credit_lots SET remaining_credits=remaining_credits-%s WHERE id=%s",
                (used, lot["id"]),
            )
            remaining -= used
            if not remaining:
                break
        if remaining:
            # Existing balances without source evidence remain unknown; no historical backfill.
            funding.append(
                {"lot_id": None, "credits": remaining, "lot_credits": remaining, "amount_fen": None}
            )
    operation_id = str(uuid4())
    conn.execute(
        "INSERT INTO billing_operations(id,user_id,service,module,source_id,billing_round,"
        "submission_id,api_key_id,"
        "auth_source,pricing_snapshot_json,unit,budget_units,reserved_credits,funding_json,"
        "request_fingerprint,collection_batch_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            operation_id,
            user_id,
            service,
            SERVICES[service].module,
            source_id,
            billing_round,
            submission_id,
            conn.api_key_id,
            conn.auth_source,
            json.dumps(snapshot),
            SERVICES[service].unit,
            amount(units),
            credits,
            json.dumps(funding),
            request_fingerprint,
            collection_batch_id,
        ),
    )
    if credits:
        _ledger(
            conn,
            operation_id,
            wallet_owner_id,
            billing_round,
            "RESERVE",
            -credits,
            credits,
            snapshot,
            actor_user_id=user_id,
        )
    return operation_id


def accept_platform_operation(
    conn: BusinessConnection,
    *,
    service: str,
    source_id: str,
    collection_batch_id: str | None = None,
    api_metadata: dict[str, str] | None = None,
) -> str:
    """Capture shared background expenditure without assigning it to a customer."""
    snapshot = retail_snapshot(conn, service, 1)
    snapshot.update(enabled=False, credits=0, free_reason="platform_service")
    operation_id = str(uuid4())
    conn.execute(
        "INSERT INTO billing_operations(id,service,module,source_id,unit,budget_units,"
        "pricing_snapshot_json,collection_batch_id,api_metadata) "
        "VALUES (%s,%s,%s,%s,%s,1,%s,%s,%s)",
        (
            operation_id,
            service,
            "viral" if collection_batch_id else "platform",
            source_id,
            SERVICES[service].unit,
            json.dumps(snapshot),
            collection_batch_id,
            json.dumps(api_metadata) if api_metadata else None,
        ),
    )
    return operation_id


def _ledger(
    conn: BusinessConnection,
    operation_id: str,
    user_id: str,
    billing_round: int,
    kind: str,
    available: int,
    reserved: int,
    snapshot: dict[str, Any],
    *,
    actor_user_id: str,
) -> None:
    """Append one wallet entry: ``user_id`` is the wallet owner, the actor is
    the operator that caused it (T2.10 — a sub-account's entry sits on the
    master's wallet with ``actor_user_id`` naming the sub)."""
    conn.execute(
        "INSERT INTO wallet_transactions(id,user_id,type,available_delta,reserved_delta,"
        "billing_operation_id,"
        "billing_round,idempotency_key,pricing_snapshot_json,api_key_id,auth_source,task_id,"
        "oral_task_id,actor_user_id,discount_rate) "
        "SELECT %s,%s,%s,%s,%s,id,%s,%s,%s,api_key_id,auth_source, "
        "CASE WHEN service IN ('video_768p','video_2k') THEN source_id END, "
        "CASE WHEN service='oral' THEN source_id END,%s,%s FROM billing_operations "
        "WHERE id=%s",
        (
            str(uuid4()),
            user_id,
            kind,
            available,
            reserved,
            billing_round,
            f"usage:{kind}:{operation_id}",
            json.dumps(snapshot),
            actor_user_id,
            snapshot_discount_rate(snapshot),
            operation_id,
        ),
    )


def find_operation(
    conn: BusinessConnection, source_id: str, *, service: str | None = None
) -> str | None:
    row = conn.execute(
        "SELECT id FROM billing_operations WHERE source_id=%s AND (%s::text IS NULL OR service=%s) "
        "ORDER BY billing_round DESC LIMIT 1",
        (source_id, service, service),
    ).fetchone()
    return str(row["id"]) if row else None


def finish_operation(
    conn: BusinessConnection,
    *,
    operation_id: str,
    units: Decimal | str | int | float,
    succeeded: bool,
    cancelled: bool = False,
) -> int:
    operation = conn.execute(
        "SELECT * FROM billing_operations WHERE id=%s FOR UPDATE", (operation_id,)
    ).fetchone()
    if operation is None:
        raise RuntimeError("计费请求不存在")
    state = "SUCCEEDED" if succeeded else "CANCELLED" if cancelled else "FAILED"
    usage = amount(units) if succeeded else Decimal(0)
    snapshot = json.loads(str(operation["pricing_snapshot_json"]))
    # Actual usage above the accepted budget is borne by the platform, never a hidden rebill.
    charged = min(int(operation["reserved_credits"]), credits_from_snapshot(snapshot, usage))
    if operation["state"] != "PENDING":
        if operation["state"] != state or Decimal(str(operation["actual_units"])) != usage:
            raise RuntimeError("计费请求已按不同结果结算")
        return int(operation["charged_credits"])
    reserved = int(operation["reserved_credits"])
    refund = reserved - charged
    if reserved:
        # Always lock the wallet before funding lots, matching acceptance lock
        # order. The payer is the master's wallet when the actor is a sub.
        wallet_owner_id = resolve_wallet_owner(conn, str(operation["user_id"]))
        conn.execute("SELECT user_id FROM wallets WHERE user_id=%s FOR UPDATE", (wallet_owner_id,))
    revenue: Decimal | None = Decimal(0)
    remaining = charged
    for part in json.loads(str(operation["funding_json"])):
        consumed = min(remaining, part["credits"])
        returned = part["credits"] - consumed
        if returned and part["lot_id"]:
            conn.execute(
                "UPDATE billing_credit_lots SET remaining_credits=remaining_credits+%s WHERE id=%s",
                (returned, part["lot_id"]),
            )
        if consumed:
            if part["amount_fen"] is None:
                revenue = None
            elif revenue is not None:
                revenue += Decimal(part["amount_fen"]) * consumed / part["lot_credits"]
        remaining -= consumed
    if reserved:
        updated = conn.execute(
            "UPDATE wallets SET available_credits=available_credits+%s,"
            "reserved_credits=reserved_credits-%s,"
            "updated_at=now() WHERE user_id=%s AND reserved_credits>=%s",
            (refund, reserved, wallet_owner_id, reserved),
        )
        if updated.rowcount != 1:
            raise RuntimeError("钱包预留积分不一致")
        if charged:
            _ledger(
                conn,
                operation_id,
                wallet_owner_id,
                int(operation["billing_round"]),
                "SETTLE",
                0,
                -charged,
                snapshot,
                actor_user_id=str(operation["user_id"]),
            )
        if refund:
            _ledger(
                conn,
                operation_id,
                wallet_owner_id,
                int(operation["billing_round"]),
                "RELEASE",
                refund,
                -refund,
                snapshot,
                actor_user_id=str(operation["user_id"]),
            )
    nominal = (
        Decimal(charged) * 100 / Decimal(str(snapshot["points_per_yuan"]))
        if snapshot["points_per_yuan"]
        else (Decimal(0) if not charged else None)
    )
    conn.execute(
        "UPDATE billing_operations SET state=%s,actual_units=%s,charged_credits=%s,revenue_fen=%s,"
        "nominal_revenue_fen=%s,completed_at=now() WHERE id=%s",
        (state, usage, charged, revenue, nominal, operation_id),
    )
    return charged


def finish_source(
    conn: BusinessConnection,
    source_id: str,
    *,
    units: Decimal | str | int | float,
    succeeded: bool,
    cancelled: bool = False,
    service: str | None = None,
) -> int | None:
    operation_id = find_operation(conn, source_id, service=service)
    if operation_id is None:
        return None
    return finish_operation(
        conn, operation_id=operation_id, units=units, succeeded=succeeded, cancelled=cancelled
    )


def begin_attempt(
    conn: BusinessConnection, *, operation_id: str, attempt_key: str, service: str | None = None
) -> str:
    operation = conn.execute(
        "SELECT service FROM billing_operations WHERE id=%s", (operation_id,)
    ).fetchone()
    if operation is None:
        raise RuntimeError("成本对应的计费请求不存在")
    subject = service or str(operation["service"])
    tariff = read_tariff(conn, subject)
    price = Decimal(0) if subject in {"cos", "zpay"} else tariff.unit_cost_fen if tariff else None
    row = conn.execute(
        "INSERT INTO billing_attempts(id,operation_id,attempt_key,service,provider,unit,"
        "unit_cost_fen) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_id,service,attempt_key) "
        "DO UPDATE SET attempt_key=excluded.attempt_key RETURNING id",
        (
            str(uuid4()),
            operation_id,
            attempt_key,
            subject,
            SERVICES[subject].provider,
            SERVICES[subject].unit,
            price,
        ),
    ).fetchone()
    return str(row["id"])


def complete_attempt(
    conn: BusinessConnection, *, attempt_id: str, usage: Decimal | str | int | float | None
) -> None:
    value = amount(usage) if usage is not None else None
    row = conn.execute(
        "SELECT * FROM billing_attempts WHERE id=%s FOR UPDATE", (attempt_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("供应商调用记录不存在")
    if row["state"] != "PENDING":
        if row["usage"] != value:
            raise RuntimeError("供应商用量与已记录结果不一致")
        return
    cost = (
        value * Decimal(str(row["unit_cost_fen"]))
        if value is not None and row["unit_cost_fen"] is not None
        else None
    )
    if value == 0:
        cost = Decimal(0)
    conn.execute(
        "UPDATE billing_attempts SET usage=%s,cost_fen=%s,state=%s,completed_at=now() WHERE id=%s",
        (value, cost, "ACTUAL" if cost is not None else "UNKNOWN", attempt_id),
    )


def record_attempt(
    conn: BusinessConnection,
    *,
    operation_id: str,
    attempt_key: str,
    usage: Decimal | str | int | float | None,
    service: str | None = None,
) -> str:
    attempt_id = begin_attempt(
        conn, operation_id=operation_id, attempt_key=attempt_key, service=service
    )
    complete_attempt(conn, attempt_id=attempt_id, usage=usage)
    return attempt_id


def begin_source_attempt(
    conn: BusinessConnection,
    source_id: str,
    *,
    service: str | None = None,
    api_metadata: dict[str, str] | None = None,
) -> str | None:
    operation = find_operation(conn, source_id)
    if operation is None:
        return None
    if api_metadata:
        # viral_data 的 API 类型挂在所属 operation 上，供管理端按类型拆解成本。
        conn.execute(
            "UPDATE billing_operations SET api_metadata=%s WHERE id=%s",
            (json.dumps(api_metadata), operation),
        )
    return begin_attempt(conn, operation_id=operation, attempt_key=str(uuid4()), service=service)


def complete_source_attempt(
    conn: BusinessConnection, source_id: str, *, usage: float | int | None
) -> None:
    operation = find_operation(conn, source_id)
    if operation is None:
        return
    row = conn.execute(
        "SELECT id FROM billing_attempts WHERE operation_id=%s AND state='PENDING' "
        "ORDER BY created_at DESC,id DESC LIMIT 1",
        (operation,),
    ).fetchone()
    if row:
        complete_attempt(conn, attempt_id=str(row[0]), usage=usage)


def _reconcile_first_frame_auxiliary_costs(conn: BusinessConnection, *, limit: int) -> None:
    """Close orphaned checks without inventing provider usage or customer charges.

    First-frame quality calls are bounded to 60 seconds. Require thirty minutes
    since both the call and task/operation completion, with no task lease. Lock
    the source task as well as the attempt so a concurrent state change cannot
    reopen work between eligibility and publication; skip busy rows entirely.
    Primary image submissions and uncertain/running tasks remain untouched.
    """
    conn.execute(
        """WITH stale AS (
            SELECT a.id FROM first_frame_tasks t
            JOIN billing_operations o ON o.source_id=t.id AND o.user_id=t.created_by_user_id
            JOIN billing_attempts a ON a.operation_id=o.id
            WHERE o.service='first_frame' AND o.state IN ('SUCCEEDED','FAILED','CANCELLED')
              AND o.completed_at < now()-interval '30 minutes'
              AND t.status IN ('SUCCEEDED','FAILED')
              AND t.locked_by IS NULL AND t.locked_until IS NULL
              AND t.completed_at::timestamptz < now()-interval '30 minutes'
              AND t.updated_at::timestamptz < now()-interval '30 minutes'
              AND a.service='quality_inspection' AND a.state='PENDING'
              AND a.usage IS NULL AND a.cost_fen IS NULL
              AND a.created_at < now()-interval '30 minutes'
            ORDER BY a.created_at,a.id LIMIT %s
            FOR UPDATE OF t,a SKIP LOCKED
        ) UPDATE billing_attempts a SET state='UNKNOWN',completed_at=now()
          FROM stale WHERE a.id=stale.id AND a.state='PENDING'""",
        (max(0, limit),),
    )


def reconcile_operations(conn: BusinessConnection, *, limit: int = 100) -> int:
    """Recover finalization after terminal task writes, including worker crashes.

    Stopped, undelivered operations release credits independently of supplier costs.
    Recoverable in-flight requests stay pending. No HTTP polling is billed.
    """
    _reconcile_first_frame_auxiliary_costs(conn, limit=limit)
    # A bounded synchronous refresh cannot still be running after thirty minutes.
    # Preserve each delivered cache update and release any unperformed remainder.
    stale = conn.execute(
        "SELECT id,COALESCE(actual_units,0) FROM billing_operations WHERE state='PENDING' "
        "AND service='viral_data' AND user_id IS NOT NULL AND created_at<now()-interval '30 "
        "minutes' "
        "ORDER BY created_at LIMIT %s",
        (limit,),
    ).fetchall()
    for row in stale:
        finish_operation(conn, operation_id=str(row[0]), units=row[1], succeeded=True)
    # A search page is an atomic delivery: the reservation settles in the same
    # transaction that persists the page, so a PENDING row past the window means
    # the page was never delivered. Release it; a retry opens a new billing round
    # through reserve_search_operation.
    searches = conn.execute(
        "SELECT id FROM billing_operations WHERE state='PENDING' "
        "AND service='viral_search' AND user_id IS NOT NULL AND created_at<now()-interval '30 "
        "minutes' "
        "ORDER BY created_at LIMIT %s",
        (limit,),
    ).fetchall()
    for row in searches:
        finish_operation(conn, operation_id=str(row[0]), units=0, succeeded=False)
    # Expired link calls cannot safely be repeated. End the receipt without resubmitting;
    # a late response cannot publish after this status CAS, so refund its user budget.
    conn.execute(
        "UPDATE viral_link_resolution_receipts SET status='UNCERTAIN', "
        "completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP "
        "WHERE status='REQUEST_SENT' AND lease_expires_at::timestamptz<now()"
    )
    conn.execute(
        "UPDATE viral_link_resolution_receipts t SET status='FAILED_SAFE', "
        "completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP "
        "WHERE status='PREPARED' AND lease_expires_at::timestamptz<now() "
        "AND EXISTS(SELECT 1 FROM billing_operations o WHERE o.source_id=t.id "
        "AND o.service='link_resolution' AND o.state='PENDING')"
    )
    # Expired paid requests are never requeued. Pending work has not been sent.
    conn.execute(
        "UPDATE prompt_optimization_receipts SET status=CASE WHEN provider_started_at IS NULL "
        "THEN 'FAILED' ELSE 'SUBMISSION_UNCERTAIN' END, completed_at=CURRENT_TIMESTAMP, "
        "updated_at=CURRENT_TIMESTAMP, error_code='PROMPT_WORKER_INTERRUPTED', "
        "error_message_redacted='优化任务中断，原文已保留。' "
        "WHERE status='RUNNING' AND lease_expires_at::timestamptz<now()"
    )
    operations = conn.execute(
        """SELECT o.id,o.service,o.source_id FROM billing_operations o WHERE o.state='PENDING'
        AND (
          EXISTS(SELECT 1 FROM generation_tasks t WHERE t.id=o.source_id AND (t.status IN
            ('FAILED','CANCELLED') OR (t.status='SUCCEEDED' AND t.actual_output_seconds IS
            NOT NULL)))
          OR EXISTS(SELECT 1 FROM oral_tasks t WHERE t.id=o.source_id AND (t.status IN
            ('FAILED','CANCELLED') OR (t.status='SUCCEEDED' AND t.duration_sec IS NOT NULL
            AND t.result_asset_id IS NOT NULL)))
          OR EXISTS(SELECT 1 FROM analysis_tasks t WHERE t.id=o.source_id AND t.status IN
            ('SUCCEEDED','FAILED','CANCELLED'))
          OR EXISTS(SELECT 1 FROM script_rewrite_tasks t WHERE t.id=o.source_id AND t.status
            IN ('SUCCEEDED','FAILED','CANCELLED','SUBMISSION_UNCERTAIN'))
          OR EXISTS(SELECT 1 FROM script_from_audio_tasks t WHERE t.id=o.source_id AND
            (t.status IN ('FAILED','CANCELLED','SUBMISSION_UNCERTAIN') OR (t.status='SUCCEEDED' AND
            t.result_json::jsonb->>'duration_sec' IS NOT NULL)))
          OR EXISTS(SELECT 1 FROM character_sheet_tasks t WHERE t.id=o.source_id AND t.status
            IN ('SUCCEEDED','FAILED','CANCELLED','SUBMISSION_UNCERTAIN'))
          OR EXISTS(SELECT 1 FROM character_generation_tasks t WHERE t.id=o.source_id AND
            t.status IN ('SUCCEEDED','FAILED','CANCELLED'))
          OR EXISTS(SELECT 1 FROM first_frame_tasks t WHERE t.id=o.source_id AND t.status IN
            ('FAILED','CANCELLED','SUBMISSION_UNCERTAIN'))
          OR EXISTS(SELECT 1 FROM oral_avatars t WHERE t.id=o.source_id AND t.status IN
            ('READY','FAILED') OR t.id=o.source_id AND t.submission_state='SUBMISSION_UNKNOWN')
          OR EXISTS(SELECT 1 FROM oral_voices t WHERE t.id=o.source_id AND t.status IN
            ('READY','FAILED') OR t.id=o.source_id AND t.submission_state='SUBMISSION_UNKNOWN')
          OR EXISTS(SELECT 1 FROM source_frame_tasks t WHERE t.id=o.source_id AND t.status IN
            ('SUCCEEDED','FAILED'))
          OR EXISTS(SELECT 1 FROM viral_link_resolution_receipts t WHERE t.id=o.source_id AND
            t.status IN ('SUCCEEDED','FAILED_SAFE','UNCERTAIN'))
          OR EXISTS(SELECT 1 FROM prompt_optimization_receipts t WHERE t.id=o.source_id AND
            t.status IN ('SUCCEEDED','FAILED','NEEDS_INPUT','SUBMISSION_UNCERTAIN'))
        ) ORDER BY o.created_at,o.id LIMIT %s
        FOR UPDATE OF o SKIP LOCKED""",
        (limit,),
    ).fetchall()
    settled = len(stale) + len(searches)
    tables = {
        "analysis": "analysis_tasks",
        "rewrite": "script_rewrite_tasks",
        "asr": "script_from_audio_tasks",
        "character": "character_sheet_tasks",
        "first_frame": "first_frame_tasks",
        "avatar_clone": "oral_avatars",
        "voice_clone": "oral_voices",
        "link_resolution": "viral_link_resolution_receipts",
        "prompt_optimize": "prompt_optimization_receipts",
        "quality_inspection": "source_frame_tasks",
    }
    for operation in operations:
        service = str(operation["service"])
        source = str(operation["source_id"])
        if service in {"video_768p", "video_2k", "oral"}:
            from app.internal_billing import finalize_internal_billing, finalize_oral_billing

            conn.execute("SAVEPOINT usage_reconcile")
            try:
                if service == "oral":
                    result = finalize_oral_billing(conn, oral_task_id=source)
                else:
                    status = conn.execute(
                        "SELECT status FROM generation_tasks WHERE id=%s", (source,)
                    ).fetchone()[0]
                    result = finalize_internal_billing(
                        conn,
                        task_id=source,
                        outcome="success"
                        if status == "SUCCEEDED"
                        else "cancelled"
                        if status == "CANCELLED"
                        else "failed",
                    )
                settled += int(result.transaction_type is not None)
            except Exception:
                conn.execute("ROLLBACK TO SAVEPOINT usage_reconcile")
            finally:
                conn.execute("RELEASE SAVEPOINT usage_reconcile")
            continue
        table = tables.get(service)
        if table is None:
            continue
        task = conn.execute(f"SELECT * FROM {table} WHERE id=%s FOR UPDATE", (source,)).fetchone()  # noqa: S608 - fixed table map
        if task is None and service == "character":
            task = conn.execute(
                "SELECT * FROM character_generation_tasks WHERE id=%s FOR UPDATE", (source,)
            ).fetchone()
        if task is None:
            continue
        status = str(task["status"])
        if (
            service in {"avatar_clone", "voice_clone"}
            and task["submission_state"] == "SUBMISSION_UNKNOWN"
        ):
            conn.execute(
                f"UPDATE {table} SET status='FAILED',lease_owner=NULL,lease_expires_at=NULL, "
                "next_attempt_at=NULL WHERE id=%s",
                (source,),
            )  # noqa: S608 - fixed clone table map
            status = "FAILED"
        if status in {
            "FAILED",
            "CANCELLED",
            "SUBMISSION_UNCERTAIN",
            "FAILED_SAFE",
            "UNCERTAIN",
            "NEEDS_INPUT",
        }:
            finish_operation(
                conn,
                operation_id=str(operation["id"]),
                units=0,
                succeeded=False,
                cancelled=status == "CANCELLED",
            )
            settled += 1
        elif status in {"SUCCEEDED", "READY"}:
            units: float | int = 1
            if service == "asr":
                payload = json.loads(str(task["result_json"] or "{}"))
                if payload.get("duration_sec") is None:
                    continue
                units = float(payload["duration_sec"])
            elif service == "first_frame":
                # Delivery count is finalized in the same publication transaction.
                continue
            finish_operation(conn, operation_id=str(operation["id"]), units=units, succeeded=True)
            settled += 1
    return settled
