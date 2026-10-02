"""记录成功交付和同批次里程碑，不能用一次授权记录代替真实读取次数。"""

from typing import Literal
from uuid import uuid4

from app.db_portable import BusinessConnection
from app.viral_content_state import ready_sql
from app.viral_homepage import live_homepage_sql


def record_content_source(
    conn: BusinessConnection,
    *,
    batch_id: str,
    source_kind: str,
    platform: str,
    video_id: str,
    keyword: str,
    is_new: bool | None = None,
) -> None:
    conn.execute(
        "INSERT INTO "
        "viral_content_sources(batch_id,source_kind,platform,video_id,keyword,prepared_at,h"
        "omepage_at,is_new) "
        f"SELECT %s,%s,v.platform,v.video_id,%s,CASE WHEN {ready_sql()} THEN now() END, "
        f"CASE WHEN {ready_sql()} AND {live_homepage_sql('v')} THEN now() END,%s "
        "FROM viral_videos v WHERE v.platform=%s AND v.video_id=%s AND v.deleted_at IS NULL "
        "ON CONFLICT DO NOTHING",
        (batch_id, source_kind, keyword, is_new, platform, video_id),
    )


def record_content_stage(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    stage: Literal["prepared", "homepage", "detail", "copy"],
) -> None:
    column = {
        "prepared": "prepared_at",
        "homepage": "homepage_at",
        "detail": "detail_at",
        "copy": "copy_at",
    }[stage]
    conn.execute(
        f"UPDATE viral_content_sources SET {column}=COALESCE({column},now()) "
        "WHERE platform=%s AND video_id=%s AND collected_at<=now() "
        + (
            "AND EXISTS (SELECT 1 FROM viral_videos v "
            "WHERE v.platform=viral_content_sources.platform "
            f"AND v.video_id=viral_content_sources.video_id AND {live_homepage_sql('v')} "
            f"AND {ready_sql()})"
            if stage == "homepage"
            else ""
        ),
        (platform, video_id),
    )


def record_customer_read(
    conn: BusinessConnection,
    *,
    platform: str,
    video_id: str,
    user_id: str,
    kind: Literal["detail", "copy"],
) -> None:
    # 管理员/审核账号的检查不能污染客户表现；授权重复读取仍是新的实际读取请求。
    role = conn.execute("SELECT role FROM users WHERE id=%s", (user_id,)).fetchone()
    if not role or role[0] not in ("user", "employee", "customer"):
        return
    conn.execute(
        "INSERT INTO viral_content_usage_events(id,platform,video_id,user_id,kind) "
        "VALUES(%s,%s,%s,%s,%s)",
        (str(uuid4()), platform, video_id, user_id, kind),
    )
    record_content_stage(conn, platform=platform, video_id=video_id, stage=kind)


def observe_due_homepages(conn: BusinessConnection) -> int:
    """记录worker首次实际观察到的展示时间，不把计划开始时间伪造成已展示历史。"""
    if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (202609260001,)).fetchone()[0]:
        return 0
    result = conn.execute(
        "UPDATE viral_content_sources s SET homepage_at=now() FROM viral_videos v "
        "WHERE s.platform=v.platform AND s.video_id=v.video_id AND s.homepage_at IS NULL "
        "AND s.collected_at<=now() AND v.deleted_at IS NULL "
        f"AND {ready_sql()} AND {live_homepage_sql('v')} "
        "AND NOT EXISTS (SELECT 1 FROM viral_video_visibility vis "
        "WHERE vis.platform=v.platform AND vis.video_id=v.video_id AND vis.status!='AVAILABLE')"
    )
    return result.rowcount
