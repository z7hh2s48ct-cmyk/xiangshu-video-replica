"""Viral billing analytics for management backend.

Statistics on TikTok Hub API calls grouped by API type,
cost attribution to platform vs customers, and per-batch breakdowns.
"""

import json
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.admin_auth_routes import AdminReader
from app.customer_fence import BusinessDbDep
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.viral_collection_billing import collection_batch_rows
from app.viral_routes import ViralStatisticsResponse, _item
from app.viral_statistics import _load_wechat_videos

router = APIRouter(tags=["viral-billing"])


class RefreshRequest(BaseModel):
    videoIds: list[str] = Field(min_length=1, max_length=12)


@router.post("/api/viral/videos/statistics/refresh", response_model=ViralStatisticsResponse)
def refresh_statistics(
    payload: RefreshRequest,
    db: BusinessDbDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> ViralStatisticsResponse:
    with db.write() as (conn, _actor):
        return ViralStatisticsResponse(
            items=[
                _item(video) for video in _load_wechat_videos(conn, sorted(set(payload.videoIds)))
            ]
        )


class BatchApiUsageDetail(BaseModel):
    """Individual API call usage within a collection batch."""

    api_type: str  # "douyin_search" | "wechat_search_page" | "wechat_video_detail"
    unit_cost: int  # in fen (cost per single unit)
    total_units: int
    total_cost_fen: int
    confirmed_count: int  # succeeded calls
    pending_count: int  # still processing
    failed_count: int  # failed calls


class CollectionBatchApiUsage(BaseModel):
    """API usage statistics for one collection batch."""

    batch_id: str
    platform: str
    created_at: str
    keywords_json: dict[str, Any]
    pricing_snapshot_json: dict[str, Any]
    douyin_search: BatchApiUsageDetail | None = None
    wechat_search_pages: BatchApiUsageDetail | None = None
    wechat_video_details: BatchApiUsageDetail | None = None
    total_cost_fen: int
    profit_fen: int | None = None  # None if calculation uncertain


@router.get(
    "/api/admin/viral/batches/{batch_id}/api-usage",
    response_model=CollectionBatchApiUsage,
)
def get_collection_batch_api_usage(
    batch_id: str,
    _actor: AdminReader,
) -> CollectionBatchApiUsage:
    """Get detailed API usage breakdown for a specific collection batch.

    This endpoint allows the admin backend to display:
    - How many times each TikTok Hub API was called
    - Cost attribution per API type
    - Success/failure rates
    """
    with pg_transaction(isolation="REPEATABLE READ") as raw:
        read_conn = BusinessConnection.postgres(raw)
        return _batch_api_usage(read_conn, batch_id)


def _batch_api_usage(read_conn: BusinessConnection, batch_id: str) -> CollectionBatchApiUsage:
    # Get basic batch info
    row = read_conn.execute(
        """
        SELECT id, platform, config_json, pricing_snapshot_json, created_at
        FROM viral_collection_batches
        WHERE id = %s
    """,
        (batch_id,),
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"Batch {batch_id} not found")

    config = json.loads(row["config_json"])
    pricing = json.loads(row["pricing_snapshot_json"])

    # Query API usage statistics grouped by API type
    result = read_conn.execute(
        """
        SELECT
            COALESCE(api_metadata->>'api_type', 'unknown') AS api_type,
            COUNT(*) AS call_count,
            SUM(
                CASE WHEN state='SUCCEEDED' AND actual_units=1
                     THEN actual_units ELSE 0 END
            ) AS confirmed_units,
            COUNT(*) FILTER(WHERE state='PENDING') AS pending_calls,
            COUNT(*) FILTER(WHERE state='FAILED') AS failed_calls,
            COALESCE(SUM(a.effective_cost_fen), 0) AS total_cost_fen
        FROM billing_operations o
        LEFT JOIN billing_effective_attempts a ON a.operation_id=o.id
        WHERE o.collection_batch_id = %s
          AND o.user_id IS NULL  -- Only platform costs
          AND service = 'viral_data'
        GROUP BY api_metadata->>'api_type'
        ORDER BY api_type
    """,
        (batch_id,),
    ).fetchall()

    # Parse the result into typed dictionaries per API type
    _empty = {
        "call_count": 0,
        "confirmed_units": 0,
        "pending_calls": 0,
        "failed_calls": 0,
        "total_cost_fen": 0,
    }
    api_type_stats: dict[str, dict[str, int]] = {
        "douyin_search": dict(_empty),
        "wechat_search_page": dict(_empty),
        "wechat_video_detail": dict(_empty),
    }

    for row in result:
        api_type = row["api_type"] or "unknown"
        if api_type in api_type_stats:
            api_type_stats[api_type].update(
                {
                    "call_count": row["call_count"],
                    "confirmed_units": row["confirmed_units"],
                    "pending_calls": row["pending_calls"],
                    "failed_calls": row["failed_calls"],
                    "total_cost_fen": row["total_cost_fen"],
                }
            )

    # Calculate totals across all API types
    total_cost_fen = sum(stats["total_cost_fen"] for stats in api_type_stats.values())

    # Get customer charges for profit calculation
    batches_with_stats = collection_batch_rows(
        read_conn,
        start=date.fromisoformat(row["created_at"][:10]),
        end=date.fromisoformat(row["created_at"][:10]) + timedelta(days=1),
        limit=1,
        offset=0,
    )

    profit_fen = None
    if batches_with_stats:
        batch_stat = batches_with_stats[0]
        profit_fen = batch_stat.get("profit_fen")
    return CollectionBatchApiUsage(
        batch_id=row["id"],
        platform=row["platform"],
        created_at=row["created_at"],
        keywords_json=config,
        pricing_snapshot_json=pricing,
        total_cost_fen=total_cost_fen,
        profit_fen=profit_fen,
        # Fill in individual API types with real data
        douyin_search=BatchApiUsageDetail(
            api_type="douyin_search",
            unit_cost=int(pricing["unit_cost_fen"]),
            total_units=api_type_stats["douyin_search"]["confirmed_units"],
            total_cost_fen=api_type_stats["douyin_search"]["total_cost_fen"],
            confirmed_count=api_type_stats["douyin_search"]["call_count"],
            pending_count=api_type_stats["douyin_search"]["pending_calls"],
            failed_count=api_type_stats["douyin_search"]["failed_calls"],
        )
        if api_type_stats["douyin_search"]["call_count"] > 0
        else None,
        wechat_search_pages=BatchApiUsageDetail(
            api_type="wechat_search_page",
            unit_cost=int(pricing["unit_cost_fen"]),
            total_units=api_type_stats["wechat_search_page"]["confirmed_units"],
            total_cost_fen=api_type_stats["wechat_search_page"]["total_cost_fen"],
            confirmed_count=api_type_stats["wechat_search_page"]["call_count"],
            pending_count=api_type_stats["wechat_search_page"]["pending_calls"],
            failed_count=api_type_stats["wechat_search_page"]["failed_calls"],
        )
        if api_type_stats["wechat_search_page"]["call_count"] > 0
        else None,
        wechat_video_details=BatchApiUsageDetail(
            api_type="wechat_video_detail",
            unit_cost=int(pricing["unit_cost_fen"]),
            total_units=api_type_stats["wechat_video_detail"]["confirmed_units"],
            total_cost_fen=api_type_stats["wechat_video_detail"]["total_cost_fen"],
            confirmed_count=api_type_stats["wechat_video_detail"]["call_count"],
            pending_count=api_type_stats["wechat_video_detail"]["pending_calls"],
            failed_count=api_type_stats["wechat_video_detail"]["failed_calls"],
        )
        if api_type_stats["wechat_video_detail"]["call_count"] > 0
        else None,
    )
