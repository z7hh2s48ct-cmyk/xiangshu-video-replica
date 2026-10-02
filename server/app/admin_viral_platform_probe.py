"""Explicit, audited paid platform probe; no ingestion or customer credit changes."""

import json
from typing import Literal
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import Field

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_viral_costs import require_cost_snapshot
from app.admin_write_contract import AdminWriteContract, write_with_idempotency
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.viral_collection_failures import FAILURES, classify_collection_failure
from app.viral_search import run_viral_search_bounded
from app.viral_tikhub import ViralSourceError, viral_source_client_from_settings

router = APIRouter()


@router.get("/viral/platforms/probes")
def read_platform_probes(_actor: AdminReader) -> dict[str, object]:
    with pg_transaction() as raw:
        rows = raw.execute(
            "SELECT platform,state,failure_code,checked_at,"
            "checked_at+interval '10 minutes'<=clock_timestamp() AS expired "
            "FROM viral_platform_probes ORDER BY platform"
        ).fetchall()
    return {
        "items": [
            {
                "platform": row[0],
                "state": "expired" if row[4] else row[1],
                "checked_at": row[3].isoformat(),
                "failure_category": FAILURES.get(row[2], FAILURES["UNKNOWN"])[0]
                if row[2]
                else None,
                "advice": FAILURES.get(row[2], FAILURES["UNKNOWN"])[1] if row[2] else None,
            }
            for row in rows
        ],
        "note": "仅显示显式探测，未探测不当作可用，10分钟后过期。",
    }


class PlatformProbeRequest(AdminWriteContract):
    expected_cost_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


@router.post("/viral/platforms/{platform}/probe")
def probe_viral_platform(
    platform: Literal["douyin", "wechat_channels"],
    payload: PlatformProbeRequest,
    request: Request,
    response: Response,
    actor: AdminWriter,
) -> dict[str, object]:
    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        require_cost_snapshot(raw, payload.expected_cost_snapshot)
        code: str | None = None
        try:
            client = viral_source_client_from_settings(BusinessConnection.postgres(raw))
            # One search page exercises the configured platform protocol. Retries
            # remain supplier requests and retain the existing platform cost meter.
            run_viral_search_bounded(client, keyword="连接验证", platform=platform)
        except (ViralSourceError, TimeoutError) as cause:
            code = classify_collection_failure(cause)
        state = "available" if code is None else "unavailable"
        row = raw.execute(
            "INSERT INTO viral_platform_probes(platform,state,failure_code,checked_at) "
            "VALUES(%s,%s,%s,clock_timestamp()) ON CONFLICT(platform) DO UPDATE SET "
            "state=excluded.state,failure_code=excluded.failure_code,"
            "checked_at=excluded.checked_at RETURNING checked_at",
            (platform, state, code),
        ).fetchone()
        raw.execute(
            "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
            "VALUES(%s,%s,'viral_platform.probe','viral_platform',%s,%s)",
            (
                str(uuid4()),
                actor.user_id,
                platform,
                json.dumps(
                    {
                        "reason": payload.reason.strip(),
                        "request_id": request_id,
                        "state": state,
                        "failure_code": code,
                        "cost_scope": (
                            "单页数据请求，重试额外计量；供应商总费用需核对，不扣客户积分。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        assert row is not None
        return {
            "platform": platform,
            "state": state,
            "checked_at": row[0].isoformat(),
            "failure_category": FAILURES[code][0] if code else None,
            "advice": FAILURES[code][1] if code else None,
            "cost_note": "供应商请求按实际计量，费用可能未知；结果仅代表本次探测，不保证后续可用。",
        }

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="VIRAL_PROBE_UNAVAILABLE",
        unavailable_message="平台探测暂不可用。",
    )
