"""客户搜索接口（P1）：一次翻页 = 一次外呼 + 一次计费 + 一次落库.

- ``POST /api/viral/search``：关键词 + 平台 + 时间范围（+ 游标）→ 命中视频 + 计费回执。
- 幂等键由 ``Idempotency-Key`` 承载，派生出 ``source_id``；同值重复请求复用
  同一计费轮次（不重复扣费），但会重新外呼（P1 不做服务端响应重放）。
- 供应商成本：``billing_context(source_id)`` 让 ``viral_tikhub._request`` 内的
  meter_call 自动落客户单的 source attempt；客户侧预留由
  ``reserve_search_operation`` 完成，落库成功后 ``persist_viral_search`` 结算。
- 响应字段与既有 ``ViralVideoItem`` 完全一致；``hasCopy`` 标记共享文案命中。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.auth import AuthenticatedUser, Database
from app.billing_catalog import SERVICES
from app.billing_meter import billing_context
from app.customer_fence import BusinessDbDep
from app.media_routes import api_base_url, get_media_storage
from app.permissions import require_not_auditor
from app.script_from_audio import cached_transcript, cached_transcripts
from app.usage_billing import finish_source
from app.viral_routes import ViralVideoItem
from app.viral_search import (
    SEARCH_SERVICE,
    archive_search_covers,
    persist_viral_search,
    reserve_search_operation,
    run_viral_search,
    search_date_shanghai,
)
from app.viral_store import list_viral_discoveries
from app.viral_tikhub import (
    ViralSourceClient,
    ViralSourceError,
    ViralSourceUnavailable,
    ViralVideo,
    viral_source_client_from_settings,
)

router = APIRouter(prefix="/api/viral", tags=["viral"])


def require_priced_viral_service(conn: Database, service: str, *, error_code: str) -> None:
    """资费 fail-closed 守卫（上线评审 H-4）。

    ``viral_search`` / ``viral_search_refresh`` 背后是真实的供应商外呼成本；
    计费目录里 tariff 缺失或未启用时，``calculate_credits`` 的既有语义是
    0 积分免费放行——漏配即「能搜但不收钱」。这两条链路必须在预留前
    显式要求已配置、已启用且单价非零的资费，否则拒绝服务。单价 0 与
    缺失同罪：管理端 tariff 路径允许写入 ``enabled=true + unit_credits=0``
    （评审 M-1），而 ``calculate_credits`` 对 0 价同样按免费放行。
    """
    from app.billing_catalog import read_tariff

    tariff = read_tariff(conn, service)
    # 三种拒绝原因必须分开报：它们的管理端处置完全不同（去配置 / 去启用 / 去改单价），
    # 笼统报「未配置」会让已经配过的人反复检查同一个地方。
    if tariff is None:
        reason = "尚未配置"
    elif not tariff.enabled:
        reason = "已停用"
    elif not tariff.unit_credits:
        # 单价 0 与缺失同罪：calculate_credits 对 0 价按免费放行，而管理端
        # tariff 路径允许写入 enabled=true + 单价 0（评审 M-1），所以这里必须拒绝。
        reason = "单价为 0"
    else:
        return
    raise HTTPException(
        status_code=503,
        detail={"code": error_code, "message": f"服务资费{reason}，请联系管理员。"},
    )


class ViralSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str = Field(min_length=1, max_length=100)
    platform: Literal["douyin", "wechat_channels"] = "douyin"
    # 与 viral_tikhub.SEARCH_TIME_RANGES 对齐的中立时间范围；缺省「一周」
    # 与历史行为（写死近 7 天）一致。
    time_range: Literal["all", "day", "week", "half_year"] = "week"
    cursor: str | None = Field(default=None, max_length=2048)


class ViralSearchBilling(BaseModel):
    charged: int
    unit: str


class ViralSearchResponse(BaseModel):
    items: list[ViralVideoItem]
    cursor: str | None
    hasMore: bool
    billing: ViralSearchBilling


def get_viral_search_source_client(conn: Database) -> ViralSourceClient | None:
    """数据源客户端依赖入口（测试可通过 dependency_overrides 替换）."""
    try:
        return viral_source_client_from_settings(conn)
    except ViralSourceUnavailable:
        return None


ViralSearchSourceClientDep = Annotated[
    ViralSourceClient | None, Depends(get_viral_search_source_client)
]


def _search_item(video: ViralVideo, *, has_copy: bool) -> ViralVideoItem:
    item = ViralVideoItem(**video.to_client_dict(), hasCopy=has_copy)
    if not video.cover_key:
        item.coverUrl = None
    if item.coverUrl and item.coverUrl.startswith("/"):
        # 自有稳定封面路由：下发绝对地址，跨源前端（桌面/开发）可直接加载。
        item.coverUrl = f"{api_base_url()}{item.coverUrl}"
    return item


@router.post("/search", response_model=ViralSearchResponse)
def search_viral_videos(
    payload: ViralSearchRequest,
    db: BusinessDbDep,
    client: ViralSearchSourceClientDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ViralSearchResponse:
    """搜索爆款视频并按次计费.

    不声明 ``AuthenticatedUser``：它会在**请求级**连接上做一次加行锁的会话校验，
    而下方多次 ``db.write()`` 会用**另一条**池连接对同一行再取锁 —— 自死锁，
    只能靠 PG 的 idle_in_transaction_session_timeout(60s) 解开。鉴权由栅栏权威
    完成（fenced_pg_transaction 在行锁下复核会话并抛 401），此处无需预解析。
    """
    key = (idempotency_key or "").strip()
    if not key or len(key) > 128:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_IDEMPOTENCY_KEY_REQUIRED",
                "message": "Idempotency-Key 请求头无效。",
            },
        )
    keyword = payload.keyword.strip()
    cursor = (payload.cursor or "").strip() or None
    if not keyword:
        raise HTTPException(
            status_code=422,
            detail={"code": "VIRAL_SEARCH_KEYWORD_REQUIRED", "message": "搜索关键词不能为空。"},
        )
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "VIRAL_SEARCH_UNAVAILABLE",
                "message": "爆款视频数据源暂不可用，请联系管理员。",
            },
        )
    fingerprint = f"{payload.platform}:{keyword}:{payload.time_range}:{cursor or ''}"
    with db.write() as (conn, actor):
        with conn:
            # 带 user_id 的 source_id 防跨用户计费串号。必须在栅栏内取 actor：
            # 外层已不再有预解析的 actor，且此处绑定的是行锁下复核过的权威身份。
            # 块外的 billing_context / 失败释放路径都引用 source_id，故在本块内先赋值。
            source_id = f"viral-search:{actor.id}:{key}"
            require_not_auditor(
                conn,
                actor=actor,
                action="viral.search",
                entity_type="viral_search",
                entity_id=payload.platform,
            )
            require_priced_viral_service(conn, SEARCH_SERVICE, error_code="VIRAL_SEARCH_UNPRICED")
            reserve_search_operation(
                conn, user_id=actor.id, source_id=source_id, request_fingerprint=fingerprint
            )
            storage = get_media_storage(conn)
    try:
        with billing_context(source_id):
            page = run_viral_search(
                client,
                keyword=keyword,
                platform=payload.platform,
                cursor=cursor,
                time_range=payload.time_range,
            )
    except ViralSourceError as exc:
        # Release reserved credits for upstream search failure
        with db.write() as (fail_conn, _fail_actor):
            with fail_conn:
                latest = fail_conn.execute(
                    "SELECT state FROM billing_operations WHERE service=%s "
                    "AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1 FOR UPDATE",
                    (SEARCH_SERVICE, source_id, actor.id),
                ).fetchone()
                if latest is not None and str(latest["state"]) == "PENDING":
                    finish_source(
                        fail_conn, source_id, units=0, succeeded=False, service=SEARCH_SERVICE
                    )
        raise HTTPException(
            status_code=503,
            detail={"code": "VIRAL_SEARCH_UPSTREAM_FAILED", "message": str(exc)},
        ) from exc
    except Exception:
        # Fallback: release reserved credits for all unexpected exceptions
        with db.write() as (fail_conn, _fail_actor):
            with fail_conn:
                latest = fail_conn.execute(
                    "SELECT state FROM billing_operations WHERE service=%s "
                    "AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1 FOR UPDATE",
                    (SEARCH_SERVICE, source_id, actor.id),
                ).fetchone()
                if latest is not None and str(latest["state"]) == "PENDING":
                    finish_source(
                        fail_conn, source_id, units=0, succeeded=False, service=SEARCH_SERVICE
                    )
        raise
    enriched = archive_search_covers(storage, page.items)
    searched_at = datetime.now(UTC).isoformat()
    with db.write() as (conn, completed_actor):
        with conn:
            if completed_actor.id != actor.id:
                raise HTTPException(status_code=401, detail={"code": "SESSION_REPLACED"})
            persist_viral_search(
                conn,
                user_id=actor.id,
                source_id=source_id,
                keyword=keyword,
                platform=payload.platform,
                videos=enriched,
                search_date=search_date_shanghai(),
                searched_at=searched_at,
            )
            charged = int(
                conn.execute(
                    "SELECT charged_credits FROM billing_operations "
                    "WHERE service=%s AND source_id=%s AND user_id=%s "
                    "ORDER BY billing_round DESC LIMIT 1",
                    (SEARCH_SERVICE, source_id, actor.id),
                ).fetchone()[0]
            )
            hits = cached_transcripts(
                conn, [(video.platform, video.video_id) for video in enriched]
            )
    return ViralSearchResponse(
        items=[
            _search_item(video, has_copy=(video.platform, video.video_id) in hits)
            for video in enriched
        ],
        cursor=page.cursor,
        hasMore=page.has_more,
        billing=ViralSearchBilling(charged=charged, unit=SERVICES["viral_search"].unit),
    )


class ViralCopyResponse(BaseModel):
    text: str | None
    updatedAt: str | None


class ViralDiscoveryItem(BaseModel):
    platform: str
    videoId: str
    keyword: str
    searchedAt: str
    video: ViralVideoItem | None


class ViralDiscoveriesResponse(BaseModel):
    date: str
    total: int
    items: list[ViralDiscoveryItem]


@router.get("/search/copy", response_model=ViralCopyResponse)
def get_viral_search_copy(
    conn: Database,
    _actor: AuthenticatedUser,
    videoId: Annotated[str, Query(min_length=1, max_length=512)],
    platform: Literal["douyin", "wechat_channels"] = "douyin",
) -> ViralCopyResponse:
    """读共享文案缓存：命即刻回填，未命中双 null（客户端再决定是否提取）.

    纯读路径：不触发下载/上传/ASR，不计费（`viral_script_cache` 跨用户共享）。
    """
    hit = cached_transcript(conn, platform=platform, video_id=videoId)
    if hit is None:
        return ViralCopyResponse(text=None, updatedAt=None)
    return ViralCopyResponse(text=hit.result.text, updatedAt=hit.updated_at)


@router.get("/search/discoveries", response_model=ViralDiscoveriesResponse)
def list_viral_search_discoveries(
    conn: Database,
    actor: AuthenticatedUser,
    search_date: Annotated[str | None, Query(alias="date", max_length=10)] = None,
) -> ViralDiscoveriesResponse:
    """客户'我的发现'（按天，默认今天·上海时区）：发现记录左联内容池."""
    resolved_date = (search_date or "").strip() or search_date_shanghai()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolved_date):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VIRAL_SEARCH_DATE_INVALID",
                "message": "日期格式应为 YYYY-MM-DD。",
            },
        )
    discoveries = list_viral_discoveries(conn, user_id=actor.id, search_date=resolved_date)
    hits = cached_transcripts(
        conn,
        [(item.platform, item.video_id) for item in discoveries if item.video is not None],
    )
    return ViralDiscoveriesResponse(
        date=resolved_date,
        total=len(discoveries),
        items=[
            ViralDiscoveryItem(
                platform=item.platform,
                videoId=item.video_id,
                keyword=item.keyword,
                searchedAt=item.searched_at,
                video=(
                    _search_item(item.video, has_copy=(item.platform, item.video_id) in hits)
                    if item.video is not None
                    else None
                ),
            )
            for item in discoveries
        ],
    )
