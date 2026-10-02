"""运营状态由可见性、素材和最新准备任务共同决定，筛选与行投影共用。"""

from app.viral_homepage import live_homepage_sql


def ready_sql(alias: str = "v") -> str:
    return (
        f"((COALESCE({alias}.cover_url,'')='' OR {alias}.cover_key IS NOT NULL) AND EXISTS "
        "(SELECT 1 FROM viral_media_preparations cs_media "
        f"WHERE cs_media.platform={alias}.platform AND cs_media.video_id={alias}.video_id "
        "AND cs_media.media_kind='video' AND cs_media.status='SUCCEEDED' "
        "AND cs_media.storage_uri IS NOT NULL))"
    )


def archive_task_status_sql(task: str, video_id: str) -> str:
    return (
        f"CASE WHEN {task}.status='FAILED' AND "
        f"{task}.collection_config_json::jsonb->>'kind'='archive_batch' THEN "
        f"CASE WHEN COALESCE({task}.checkpoint_json::jsonb->'failed_video_ids','[]'::jsonb) "
        f"@> jsonb_build_array({video_id}) THEN 'FAILED' ELSE 'SUCCEEDED' END "
        f"ELSE {task}.status END"
    )


def archive_task_match_sql(task: str, video: str) -> str:
    return (
        f"(({task}.collection_config_json::jsonb->>'kind'='single_archive' AND "
        f"{task}.collection_config_json::jsonb->>'video_id'={video}.video_id) OR "
        f"({task}.collection_config_json::jsonb->>'kind'='archive_batch' AND "
        f"{task}.collection_config_json::jsonb->'video_ids' "
        f"@> jsonb_build_array({video}.video_id)))"
    )


def content_state_sql(alias: str = "v") -> str:
    visibility = (
        "COALESCE((SELECT status FROM viral_video_visibility cs_visibility "
        f"WHERE cs_visibility.platform={alias}.platform "
        f"AND cs_visibility.video_id={alias}.video_id),'AVAILABLE')"
    )
    task = (
        f"(SELECT {archive_task_status_sql('cs_task', f'{alias}.video_id')} "
        "FROM viral_refresh_tasks cs_task "
        f"WHERE cs_task.platform={alias}.platform AND cs_task.sort='latest' "
        f"AND {archive_task_match_sql('cs_task', alias)} "
        "ORDER BY cs_task.created_at DESC,cs_task.id DESC LIMIT 1)"
    )
    failed = (
        "EXISTS (SELECT 1 FROM viral_media_preparations cs_failed "
        f"WHERE cs_failed.platform={alias}.platform AND cs_failed.video_id={alias}.video_id "
        "AND cs_failed.media_kind='video' AND cs_failed.status='FAILED')"
    )
    ready = ready_sql(alias)
    return (
        f"CASE WHEN {alias}.deleted_at IS NOT NULL THEN 'blocked' "
        f"WHEN {visibility} IN ('HIDDEN','UNAVAILABLE') THEN 'removed' "
        f"WHEN {ready} AND {live_homepage_sql(alias)} THEN 'featured' "
        f"WHEN {ready} THEN 'ready' WHEN {task} IN ('PENDING','RUNNING') THEN 'pending_prepare' "
        f"WHEN {failed} OR {task}='FAILED' THEN 'prepare_failed' ELSE 'pending_prepare' END"
    )


def customer_visible_sql(alias: str = "v") -> str:
    # 与客户列表同样使用数据库时钟；旧采集快照不延长内容可见期。
    end = "floor(extract(epoch FROM now()))"
    return (
        f"({ready_sql(alias)} AND {alias}.deleted_at IS NULL AND NOT EXISTS "
        f"(SELECT 1 FROM viral_video_visibility cs_vis WHERE cs_vis.platform={alias}.platform "
        f"AND cs_vis.video_id={alias}.video_id AND cs_vis.status!='AVAILABLE') "
        f"AND regexp_replace({alias}.title,'<em[^>]*>|</em>','','gi') "
        r"!~* 'minecraft|我的世界|\mmc\M' "
        f"AND ({live_homepage_sql(alias)} OR ({alias}.collection_published=1 "
        f"AND {alias}.published_at "
        f"BETWEEN ({end})-604800 AND ({end}))))"
    )
