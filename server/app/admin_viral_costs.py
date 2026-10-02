"""管理端操作前的只读估算；与实际数据请求共用成本科目。"""

import hashlib
import json
from typing import Literal

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.admin_auth_routes import AdminReader
from app.admin_write_contract import http_error
from app.billing_catalog import read_tariff
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

router = APIRouter(prefix="/viral", tags=["admin-runtime"])


class CostTarget(BaseModel):
    platform: Literal["douyin", "wechat_channels"]
    video_id: str = Field(min_length=1, max_length=2048)


class OperationEstimateRequest(BaseModel):
    action: Literal["search", "prepare", "statistics", "feature", "archive"]
    platform: Literal["douyin", "wechat_channels"] = "douyin"
    items: list[CostTarget] = Field(default_factory=list, max_length=50)


def cost_snapshot(conn: BusinessConnection) -> tuple[str, float | None]:
    tariff = read_tariff(conn, "viral_data")
    price = tariff.unit_cost_fen if tariff else None
    state = {
        "service": "viral_data",
        "price": str(price) if price is not None else None,
        "version": tariff.version if tariff else None,
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(), (
        float(price) if price is not None else None
    )


def require_cost_snapshot(raw: psycopg.Connection, expected: str | None) -> None:
    if expected is not None and cost_snapshot(BusinessConnection.postgres(raw))[0] != expected:
        raise http_error(409, "VIRAL_COST_CHANGED", "接口成本单价已改变，请重新预估后确认。")


def operation_estimate(
    conn: BusinessConnection, payload: OperationEstimateRequest
) -> dict[str, object]:
    from app.viral_statistics import _needs_refresh
    from app.viral_store import get_viral_video

    snapshot, price = cost_snapshot(conn)
    calls = 1 if payload.action == "search" else 0
    reused = 0
    media = 0
    seen: set[tuple[str, str]] = set()
    for target in payload.items:
        identity = (target.platform, target.video_id)
        if identity in seen:
            continue
        seen.add(identity)
        video = get_viral_video(conn, platform=target.platform, video_id=target.video_id)
        if video is None:
            raise http_error(404, "VIRAL_VIDEO_NOT_FOUND", "预估的视频不存在，请刷新列表。")
        if payload.action == "statistics":
            if (
                target.platform == "wechat_channels"
                and _needs_refresh(video)
                and (video.native.get("export_id") or video.native.get("_statistics_object_id"))
            ):
                calls += 1
            else:
                reused += 1
        else:
            ready = conn.execute(
                "SELECT 1 FROM viral_media_preparations WHERE platform=%s "
                "AND video_id=%s AND media_kind='video' AND status='SUCCEEDED' "
                "AND storage_uri IS NOT NULL",
                identity,
            ).fetchone()
            if ready:
                reused += 1
            else:
                media += 1
                calls += int(target.platform == "wechat_channels")
    # 单条详情进程缓存可能减少请求；后台续跑与断机结果未知，不能给最终费用上限。
    normal_max = calls * 3
    return {
        "action": payload.action,
        "service": "viral_data",
        "snapshot": snapshot,
        "unitCostFen": price,
        "logicalCallsMax": calls,
        "normalRetryCallsMax": normal_max,
        "dataCostMaxFen": price * normal_max if price is not None else None,
        "reusedVideos": reused,
        "mediaDownloadsMax": media,
        "storageCostFen": None,
        "transferCostFen": None,
        "totalCostFen": None,
        "customerCredits": 0,
        "note": (
            "按当前数据接口成本单价估算；缓存、过滤和复用会降低请求次数。"
            "常规最多3次物理尝试；后台续跑、断机及重定向次数不在此范围内，最终上限未知。"
            "下载、存储和流量未计量，总费用未知。管理端本操作不扣客户积分。"
        ),
    }


@router.post("/operations/estimate")
def estimate_operation(payload: OperationEstimateRequest, _actor: AdminReader) -> dict[str, object]:
    with pg_transaction() as raw:
        return operation_estimate(BusinessConnection.postgres(raw), payload)
