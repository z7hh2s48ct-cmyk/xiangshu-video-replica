"""视频直链刷新接口（P1 补充）：按需刷新单条视频的封面/播放地址等资源.

- ``POST /api/viral/search/refresh``：视频 ID + 平台 → 重新外呼 + 封面归档 + upsert。
- 每次刷新消耗 1 单位 credits（从 tariff 读取）。
- 幂等键由 ``Idempotency-Key`` 承载，派生出 ``source_id``。
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.auth import Database
from app.billing_catalog import SERVICES
from app.billing_meter import billing_context
from app.customer_fence import BusinessDbDep
from app.media_routes import api_base_url, get_media_storage
from app.permissions import require_not_auditor
from app.usage_billing import finish_source
from app.viral_search import archive_search_covers
from app.viral_search_routes import require_priced_viral_service
from app.viral_store import get_viral_video, update_viral_cover, upsert_viral_videos
from app.viral_tikhub import (
    ViralSourceClient,
    ViralSourceError,
    ViralSourceUnavailable,
    ViralVideo,
    viral_source_client_from_settings,
)

router = APIRouter(prefix="/api/viral", tags=["viral"])


class ViralSearchRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: str = Field(min_length=1, max_length=50)
    videoId: str = Field(min_length=1, max_length=200)


class ViralSearchRefreshBilling(BaseModel):
    charged: int
    unit: str


class ViralSearchRefreshResponse(BaseModel):
    video: dict[str, Any]
    billing: ViralSearchRefreshBilling


REFRESH_SERVICE = "viral_search_refresh"


def get_viral_search_refresh_source_client(conn: Database) -> ViralSourceClient | None:
    """数据源客户端依赖入口（测试可通过 dependency_overrides 替换）."""
    try:
        return viral_source_client_from_settings(conn)
    except ViralSourceUnavailable:
        return None


ViralSearchRefreshSourceClientDep = Annotated[
    ViralSourceClient | None, Depends(get_viral_search_refresh_source_client)
]


def _search_item(video: ViralVideo, *, has_copy: bool) -> dict[str, Any]:
    """将 ViralVideo 转换为搜索结果项格式."""
    item: dict[str, Any] = {
        "platform": video.platform,
        "videoId": video.video_id,
        "title": video.title,
        "author": video.author,
        "coverUrl": None,
    }
    if video.cover_key:
        item["coverUrl"] = f"{api_base_url()}/viral/covers/{video.platform}/{video.video_id}"
    elif video.cover_url:
        # Fallback to original URL
        item["coverUrl"] = video.cover_url
    return item


@router.post("/search/refresh", response_model=ViralSearchRefreshResponse)
def refresh_viral_video(
    payload: ViralSearchRefreshRequest,
    db: BusinessDbDep,
    client: ViralSearchRefreshSourceClientDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralSearchRefreshResponse:
    """刷新单条视频资源.

    不声明 ``AuthenticatedUser``：它会在**请求级**连接上做一次加行锁的会话校验
    （auth.py 的 verify_session_context 走 FOR UPDATE），而下方 ``db.write()`` 的
    栅栏事务会用**另一条**池连接对同一行再取一次锁 —— 自死锁，靠 PG 的
    idle_in_transaction_session_timeout(60s) 才解开。鉴权由栅栏权威完成
    （fenced_pg_transaction 在行锁下复核会话并抛 401），此处无需预解析。
    """
    from app.usage_billing import accept_operation

    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_REFRESH_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    # 上线评审 H-3：刷新按「视频 ID 当关键词跑抖音通用搜索」模拟实现，只能服务
    # 抖音且必须精确匹配返回的 video_id；视频号没有 detail→ViralVideo 的映射链路，
    # 拿抖音搜索顶替会把无关视频写进内容池并按成功扣费。其它平台在预留计费前拒绝。
    if payload.platform != "douyin":
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_REFRESH_PLATFORM_UNSUPPORTED",
                "message": "该平台的视频刷新暂不支持。",
            },
        )
    with db.write() as (conn, actor):
        with conn:
            require_not_auditor(
                conn,
                actor=actor,
                action="viral.refresh",
                entity_type="viral_refresh",
                entity_id=payload.platform,
            )
            # 检查视频是否存在 (包括已删除的)
            video = get_viral_video(conn, platform=payload.platform, video_id=payload.videoId)
            if video is None:
                # 尝试获取已删除的视频
                row = conn.execute(
                    """
                    SELECT * FROM viral_videos
                    WHERE platform = %s AND video_id = %s
                    """,
                    (payload.platform, payload.videoId),
                ).fetchone()
                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "code": "VIRAL_VIDEO_NOT_FOUND",
                            "message": "视频不存在。",
                        },
                    )
                # 用下标取列：行对象是 db_portable._NamedRow，它仿 sqlite3.Row 只提供
                # row["col"] 与 keys()，没有 .get()（全仓无此写法）。列由
                # 20260913T1600_shared_viral_media 建立在 viral_videos 上，SELECT * 必含。
                if row["deleted_at"] is not None:
                    raise HTTPException(
                        status_code=403,
                        detail={
                            "code": "VIRAL_VIDEO_FORBIDDEN_DELETED",
                            "message": "视频已被删除，无法刷新。",
                        },
                    )
            if client is None:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "VIRAL_SEARCH_REFRESH_UNAVAILABLE",
                        "message": "数据源暂不可用，请联系管理员。",
                    },
                )
            require_priced_viral_service(
                conn, REFRESH_SERVICE, error_code="VIRAL_SEARCH_REFRESH_UNPRICED"
            )
            source_id = f"viral-refresh:{actor.id}:{key}"
            fingerprint = f"{payload.platform}:{payload.videoId}:{key}"
            # 预留计费
            latest = conn.execute(
                "SELECT billing_round, state FROM billing_operations "
                "WHERE user_id=%s AND service=%s "
                "AND source_id=%s ORDER BY billing_round DESC LIMIT 1",
                (actor.id, REFRESH_SERVICE, source_id),
            ).fetchone()
            billing_round = 1
            if latest is not None:
                billing_round = int(latest["billing_round"])
                if str(latest["state"]) in {"FAILED", "CANCELLED"}:
                    billing_round += 1
            # 返回值按仓库既有惯例丢弃：结算走 source_id + service 定位
            # （见 usage_billing.finish_source），与其余 10 处调用点一致。
            accept_operation(
                conn,
                user_id=actor.id,
                service=REFRESH_SERVICE,
                source_id=source_id,
                units=1,
                billing_round=billing_round,
                request_fingerprint=fingerprint,
            )
            storage = get_media_storage(conn)
    try:
        with billing_context(source_id):
            # 重新外呼获取最新数据
            videos = client.douyin_refresh(platform=payload.platform, video_id=payload.videoId)
            # 搜索是按「ID 当关键词」模拟的：只有返回列表里精确命中请求的
            # video_id 才算拿到本视频的数据；任何其它结果都是无关视频，
            # 绝不能入库或结算（走下方 ViralSourceError 释放预留）。
            enriched_video = next(
                (video for video in videos if video.video_id == payload.videoId), None
            )
            if enriched_video is None:
                raise ViralSourceError(
                    f"No matching record returned for {payload.platform}:{payload.videoId}"
                )
    except ViralSourceError as exc:
        # 外呼失败释放预留
        with db.write() as (fail_conn, _fail_actor):
            with fail_conn:
                latest = fail_conn.execute(
                    "SELECT state FROM billing_operations WHERE service=%s "
                    "AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1 FOR UPDATE",
                    (REFRESH_SERVICE, source_id, actor.id),
                ).fetchone()
                if latest is not None and str(latest["state"]) == "PENDING":
                    finish_source(
                        fail_conn, source_id, units=0, succeeded=False, service=REFRESH_SERVICE
                    )
        raise HTTPException(
            status_code=503,
            detail={"code": "VIRAL_SEARCH_REFRESH_UPSTREAM_FAILED", "message": str(exc)},
        ) from exc
    except Exception:
        # Fallback: release reserved credits for all unexpected exceptions
        with db.write() as (fail_conn, _fail_actor):
            with fail_conn:
                latest = fail_conn.execute(
                    "SELECT state FROM billing_operations WHERE service=%s "
                    "AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1 FOR UPDATE",
                    (REFRESH_SERVICE, source_id, actor.id),
                ).fetchone()
                if latest is not None and str(latest["state"]) == "PENDING":
                    finish_source(
                        fail_conn, source_id, units=0, succeeded=False, service=REFRESH_SERVICE
                    )
        raise
    # 封面归档并 upsert 视频数据
    enriched = archive_search_covers(storage, [enriched_video])
    updated_video = enriched[0]
    with db.write() as (conn, completed_actor):
        with conn:
            if completed_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            upsert_viral_videos(conn, [updated_video], commit=False)
            if updated_video.cover_key:
                update_viral_cover(
                    conn,
                    platform=updated_video.platform,
                    video_id=updated_video.video_id,
                    cover_key=updated_video.cover_key,
                    commit=False,
                )
            # 成功交付必须结算：与 persist_viral_search 同一约定。原先只读
            # charged_credits 而不结算，计费单永远停在 PENDING —— 返回值恒为 0，
            # 且预留在 reserved_credits 里永不释放（客户积分被长期占住）。
            finish_source(conn, source_id, units=1, succeeded=True, service=REFRESH_SERVICE)
            charged = int(
                conn.execute(
                    "SELECT charged_credits FROM billing_operations "
                    "WHERE service=%s AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1",
                    (REFRESH_SERVICE, source_id, actor.id),
                ).fetchone()[0]
            )
    return ViralSearchRefreshResponse(
        video=_search_item(updated_video, has_copy=False),
        billing=ViralSearchRefreshBilling(charged=charged, unit=SERVICES[REFRESH_SERVICE].unit),
    )
