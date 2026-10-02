"""Read-only customer operations and activity facts; no pricing or balance mutations."""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException

CustomerAttention = Literal["", "low_balance", "recent_failure", "inactive"]


def utc_text_timestamp(column: str) -> str:
    """Legacy naive strings mean UTC; preserve explicit offsets from the PG clock."""
    return (
        f"({column} || CASE WHEN {column} ~ '(Z|[+-][0-9]{{2}}(:?[0-9]{{2}})?)$' "
        "THEN '' ELSE '+00:00' END)::timestamptz"
    )


# Match management generation ownership. Payment orders are never generation jobs.
_TASK_SOURCES = (
    ("first_frame_tasks", "created_by_user_id", "首帧生成"),
    ("character_sheet_tasks", "created_by_user_id", "角色设定"),
    ("character_generation_tasks", "created_by", "角色生成"),
    ("source_frame_tasks", "created_by_user_id", "素材首帧"),
    ("analysis_tasks", "created_by_user_id", "视频分析"),
    ("oral_tasks", "owner_user_id", "口播视频"),
)
_FACT_BRANCHES = [
    "SELECT gt.id, gb.created_by_user_id AS user_id, '视频生成'::text AS label, "
    f"{utc_text_timestamp('gt.created_at')} AS created_at, "
    "(gt.status='SUCCEEDED' AND gt.archive_status IN ('ARCHIVED','DIRECT')) AS succeeded, "
    "(gt.status IN ('FAILED','CANCELLED','SUBMISSION_UNCERTAIN') "
    "OR gt.archive_status='ARCHIVE_FAILED' "
    "OR gt.quality_status IN ('AUDIO_QUALITY_FAILED','VISUAL_QUALITY_FAILED')) AS failed "
    "FROM generation_tasks gt JOIN generation_batches gb ON gb.id=gt.batch_id"
]
for _table, _owner, _label in _TASK_SOURCES:
    _FACT_BRANCHES.append(
        f"SELECT id, {_owner}, '{_label}', {utc_text_timestamp('created_at')}, "
        "(status='SUCCEEDED'), "
        "(status IN ('FAILED','CANCELLED','SUBMISSION_UNCERTAIN','ARCHIVE_FAILED')) "
        f"FROM {_table}"
    )
for _table, _label in [("oral_avatars", "数字人克隆"), ("oral_voices", "音色克隆")]:
    _FACT_BRANCHES.append(
        f"SELECT id, owner_user_id, '{_label}', {utc_text_timestamp('created_at')}, "
        "(status='READY' AND submission_state IS DISTINCT FROM 'SUBMISSION_UNKNOWN'), "
        "(status IN ('FAILED','CANCELLED') OR submission_state='SUBMISSION_UNKNOWN') "
        f"FROM {_table}"
    )

CUSTOMER_FACTS_CTE = "WITH customer_task_facts AS (" + " UNION ALL ".join(_FACT_BRANCHES) + "), "
CUSTOMER_FACTS_CTE += f"""
customer_activity_facts AS (
 SELECT user_id, {utc_text_timestamp("created_at")} AS created_at
 FROM wallet_transactions
 UNION ALL SELECT user_id, created_at FROM customer_task_facts
 UNION ALL SELECT user_id, created_at::timestamptz FROM customer_session_events
 WHERE event IN ('LOGIN','HEARTBEAT')
)
"""
CUSTOMER_METRICS_JOINS = """
 LEFT JOIN (
   SELECT user_id, COUNT(*) AS total_30d,
     COUNT(*) FILTER (WHERE succeeded) AS succeeded_30d,
     COUNT(*) FILTER (WHERE failed) AS failed_30d,
     COUNT(*) FILTER (WHERE failed AND created_at >= now()-interval '7 days') AS failed_7d
   FROM customer_task_facts
   WHERE created_at >= now()-interval '30 days' AND created_at <= now()
   GROUP BY user_id
 ) recent ON recent.user_id=u.id
 LEFT JOIN (
   SELECT user_id, MAX(created_at) AS last_at FROM customer_activity_facts
   WHERE created_at <= now() GROUP BY user_id
 ) activity ON activity.user_id=u.id
"""
INACTIVE_SQL = (
    f"COALESCE(activity.last_at, {utc_text_timestamp('u.created_at')}) < now()-interval '30 days'"
)


def append_attention_filter(
    clauses: list[str], params: list[object], attention: CustomerAttention, threshold: int | None
) -> None:
    if attention == "low_balance":
        if threshold is None:
            raise HTTPException(400, detail={"code": "BALANCE_THRESHOLD_REQUIRED"})
        clauses.append("COALESCE(w.available_credits, 0) < %s")
        params.append(threshold)
    elif attention == "recent_failure":
        clauses.append("COALESCE(recent.failed_7d,0)>0")
    elif attention == "inactive":
        clauses.append(INACTIVE_SQL)
