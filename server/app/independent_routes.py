"""独立视频创作路由（C2）：能力查询 + 幂等建批。

骨架对齐 oral_routes：读路由用只读 ``Database``，写路由走
``BusinessDbDep`` 的 fenced 写事务。批次创建后的一切（列表/详情/取消/
预览/下载/对账）复用既有 generation 端点，本文件不重复暴露。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.auth import Database
from app.customer_fence import BusinessDbDep
from app.generation import BatchResult
from app.independent import (
    IndependentCapabilities,
    IndependentVideoRequest,
    create_independent_batch,
    read_independent_capabilities,
)

router = APIRouter(prefix="/api/independent")


@router.get("/capabilities", response_model=IndependentCapabilities)
def read_capabilities(conn: Database) -> IndependentCapabilities:
    """视频生成页的能力探测：扩展模式是否开放、单批数量上限。"""
    return read_independent_capabilities(conn)


@router.post("/video-tasks", response_model=BatchResult, status_code=201)
def create_video_task(
    request: IndependentVideoRequest,
    db: BusinessDbDep,
) -> BatchResult:
    """幂等创建独立创作批次（含钱包按秒预留与公平队列入列）。"""
    with db.write() as (conn, actor):
        return create_independent_batch(conn, actor=actor, request=request)
