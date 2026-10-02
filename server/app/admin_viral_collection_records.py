"""Business execution history, independent of mutable platform/sort queue rows."""

import json
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.admin_auth_routes import AdminReader
from app.billing_reports import date_bounds
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.sql_pagination import PAGE_CLAUSE
from app.viral_collection_billing import create_collection_batch
from app.viral_collection_failures import FAILURES

router = APIRouter()


def start_admin_search_record(platform: str, keyword: str) -> str:
    # Search failures roll back the write-contract transaction. Keep this outcome
    # separately, just as each physical supplier request has its own durable bill.
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        batch = create_collection_batch(
            conn,
            platform=platform,
            config={"trigger_kind": "realtime", "keywords": [{"keyword": keyword}]},
            user_ids=[],
        )
        conn.execute(
            "UPDATE viral_collection_batches SET run_status='RUNNING',started_at=clock_timestamp(),"
            "lease_expires_at=clock_timestamp()+interval '10 minutes' WHERE id=%s",
            (batch,),
        )
        return batch


def fail_admin_search_record(batch: str, reason: str, code: str = "UNKNOWN") -> None:
    with pg_transaction() as raw:
        raw.execute(
            "UPDATE viral_collection_batches SET run_status='FAILED',"
            "completed_at=clock_timestamp(),"
            "failure_reason=%s,failure_code=%s WHERE id=%s AND run_status='RUNNING'",
            (reason, code, batch),
        )


def recover_interrupted_search_records(conn: BusinessConnection) -> int:
    result = conn.execute(
        "UPDATE viral_collection_batches SET run_status='FAILED',completed_at=clock_timestamp(),"
        "failure_code='INTERRUPTED',failure_reason=%s WHERE run_status='RUNNING' "
        "AND config_json::jsonb->>'trigger_kind'='realtime' "
        "AND lease_expires_at<=clock_timestamp()",
        (FAILURES["INTERRUPTED"][0],),
    )
    return result.rowcount


@router.get("/viral/collection/records")
def read_collection_records(
    _actor: AdminReader,
    start: date,
    end: date,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    lower, upper = date_bounds(start, end)
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        rows = conn.execute(
            f"""WITH sources AS (
          SELECT batch_id,count(DISTINCT (platform,video_id)) AS videos,
            count(DISTINCT (platform,video_id)) FILTER(WHERE is_new) AS new_videos,
            count(*) FILTER(WHERE is_new IS NULL) AS unknown_new,
            count(DISTINCT (platform,video_id)) FILTER(WHERE prepared_at IS NOT NULL) AS ready
          FROM viral_content_sources GROUP BY batch_id
        ), keywords AS (
          SELECT batch_id,count(*) FILTER(WHERE status='SUCCEEDED') AS succeeded,
            count(*) FILTER(WHERE status='FAILED') AS failed
          FROM viral_keyword_runs GROUP BY batch_id
        ), costs AS (
          SELECT o.collection_batch_id AS batch_id,count(*) AS calls,
            sum(a.effective_cost_fen) AS known,
            count(*) FILTER(WHERE a.effective_cost_fen IS NULL AND a.state<>'PENDING') AS unknown,
            count(*) FILTER(WHERE a.state='PENDING') AS pending
          FROM billing_effective_attempts a JOIN billing_operations o ON o.id=a.operation_id
          WHERE o.user_id IS NULL AND o.collection_batch_id IS NOT NULL
          GROUP BY o.collection_batch_id
        ) SELECT b.*,s.videos,s.new_videos,s.unknown_new,s.ready,k.succeeded,k.failed,
          COALESCE(c.calls,0) AS calls,COALESCE(c.known,0) AS known_cost_fen,
          COALESCE(c.unknown,0) AS unknown_cost_count,COALESCE(c.pending,0) AS pending_cost_count,
          count(*) OVER() AS total FROM viral_collection_batches b
          LEFT JOIN sources s ON s.batch_id=b.id LEFT JOIN keywords k ON k.batch_id=b.id
          LEFT JOIN costs c ON c.batch_id=b.id WHERE b.created_at>=%s AND b.created_at<%s
          ORDER BY b.created_at DESC,b.id DESC {PAGE_CLAUSE}""",
            (lower, upper, limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        config = json.loads(item.pop("config_json"))
        item.pop("pricing_snapshot_json")
        item.pop("total")
        item.pop("video_outcomes_json", None)
        item["trigger_kind"] = config.get("trigger_kind")
        item["keywords"] = [word.get("keyword", "") for word in config.get("keywords", [])]
        if item["stats_version"] == 1:
            for field in ["videos", "new_videos", "unknown_new", "ready"]:
                item[field] = item[field] or 0
        else:
            # 旧续跑即便有部分新来源，也不足以代表整个批次的接收/新增/就绪量。
            for field in ["videos", "new_videos", "unknown_new", "ready"]:
                item[field] = None
        item["new_count"] = item["new_videos"] if item["unknown_new"] == 0 else None
        item["cost_fen"] = (
            None
            if item["unknown_cost_count"] or item["pending_cost_count"]
            else item["known_cost_fen"]
        )
        if item["run_status"] in (None, "PENDING", "RUNNING"):
            item["cost_fen"] = None
        if (
            item["run_status"] in ("SUCCEEDED", "FAILED")
            and not item["calls"]
            and item["failure_code"] != "CONFIGURATION"
        ):
            item["cost_fen"] = None
            item["unknown_cost_count"] += 1
        category = FAILURES.get(item["failure_code"], FAILURES["UNKNOWN"])
        item["failure_category"] = category[0] if item["run_status"] == "FAILED" else None
        item["advice"] = category[1] if item["run_status"] == "FAILED" else None
        item["failure_reason"] = category[0] if item["run_status"] == "FAILED" else None
        for field in ["created_at", "started_at", "completed_at", "lease_expires_at"]:
            item[field] = item[field].isoformat() if item[field] else None
        items.append(item)
    return {
        "items": items,
        "total": int(rows[0]["total"]) if rows else 0,
        "offset": offset,
        "limit": limit,
        "countingRule": (
            "每个平台批次或每次实时搜索独立留档；视频去重，新入库以实际INSERT为准，"
            "历史新增数未知。费用是该批次供应商接口成本，下载与存储费另行核对。"
        ),
    }


@router.get("/viral/collection/records/{batch_id}/tasks")
def read_collection_record_tasks(
    batch_id: str,
    _actor: AdminReader,
    video_limit: Annotated[int, Query(ge=1, le=50)] = 20,
    video_offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        batch = conn.execute(
            "SELECT id,platform,related_task_id,run_status,failure_code,video_outcomes_json "
            "FROM viral_collection_batches WHERE id=%s",
            (batch_id,),
        ).fetchone()
        if batch is None:
            raise HTTPException(
                404, detail={"code": "VIRAL_RECORD_NOT_FOUND", "message": "采集记录不存在。"}
            )
        runs = conn.execute(
            "SELECT platform,keyword,status,video_count,started_at,finished_at,"
            "attempt_count,failure_code "
            "FROM viral_keyword_runs WHERE batch_id=%s ORDER BY started_at,platform,keyword",
            (batch_id,),
        ).fetchall()
        queue = conn.execute(
            "SELECT id,status,attempt,retry_count FROM viral_refresh_tasks WHERE id=%s "
            "AND collection_config_json::jsonb->>'billing_batch_id'=%s",
            (batch["related_task_id"], batch_id),
        ).fetchone()
    outcomes = sorted(
        json.loads(batch["video_outcomes_json"]).items(),
        key=lambda item: (item[1].get("status") != "FAILED", item[0]),
    )
    return {
        "batch_id": batch_id,
        "video_total": len(outcomes),
        "video_limit": video_limit,
        "video_offset": video_offset,
        "related_task_id": batch["related_task_id"],
        "current_queue": dict(queue) if queue else None,
        "videos": [
            {
                "platform": batch["platform"],
                "video_id": video_id,
                "title": outcome.get("title") or "未命名视频",
                "status": outcome.get("status"),
                "failure_category": FAILURES.get(outcome.get("failure_code"), FAILURES["UNKNOWN"])[
                    0
                ]
                if outcome.get("status") == "FAILED"
                else None,
                "advice": FAILURES.get(outcome.get("failure_code"), FAILURES["UNKNOWN"])[1]
                if outcome.get("status") == "FAILED"
                else None,
            }
            for video_id, outcome in outcomes[video_offset : video_offset + video_limit]
        ],
        "keywords": [
            {
                **dict(row),
                "failure_category": FAILURES.get(row["failure_code"], FAILURES["UNKNOWN"])[0]
                if row["status"] == "FAILED"
                else None,
                "advice": FAILURES.get(row["failure_code"], FAILURES["UNKNOWN"])[1]
                if row["status"] == "FAILED"
                else None,
            }
            for row in runs
        ],
        "note": (
            "关联任务编号永久保留；队列复用后不展示其他批次的结果。实时搜索直接执行，没有队列任务。"
        ),
    }
