"""首页排期只使用数据库时钟，客户端读取与管理端预览共用判据。"""

from __future__ import annotations


def live_homepage_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}homepage_featured=1 "
        f"AND ({prefix}homepage_starts_at IS NULL OR {prefix}homepage_starts_at<=now()) "
        f"AND ({prefix}homepage_ends_at IS NULL OR {prefix}homepage_ends_at>now()))"
    )


def pending_homepage_sql(alias: str = "v") -> str:
    return (
        "EXISTS (SELECT 1 FROM viral_refresh_tasks home_task "
        f"WHERE home_task.platform={alias}.platform "
        "AND home_task.status IN ('PENDING','RUNNING','FAILED') "
        "AND (home_task.status!='FAILED' OR home_task.retryable=1) "
        "AND home_task.collection_config_json::jsonb->>'kind'='archive_batch' "
        "AND home_task.collection_config_json::jsonb->'feature_after'->'versions' "
        f"->>{alias}.video_id={alias}.homepage_intent_version::text)"
    )
