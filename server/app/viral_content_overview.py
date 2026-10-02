"""同采集批次转化与业务台账汇总；历史未观测数据不推断、不分摊。"""

from datetime import date

from app.billing_reports import date_bounds
from app.db_portable import BusinessConnection


def content_overview(conn: BusinessConnection, *, start: date, end: date) -> dict[str, object]:
    lower, upper = date_bounds(start, end)
    started = conn.execute(
        "SELECT started_at FROM viral_content_measurement_state WHERE id=1"
    ).fetchone()[0]
    counts = conn.execute(
        """WITH cohort AS (
          SELECT platform,video_id,bool_or(prepared_at IS NOT NULL) AS prepared,
            bool_or(homepage_at IS NOT NULL) AS featured,bool_or(detail_at IS NOT NULL) AS detail,
            bool_or(copy_at IS NOT NULL) AS copy FROM viral_content_sources
          WHERE collected_at>=%s AND collected_at<%s GROUP BY platform,video_id
        ) SELECT count(*),count(*) FILTER(WHERE prepared),
          count(*) FILTER(WHERE prepared AND featured),
          count(*) FILTER(WHERE prepared AND featured AND detail),
          count(*) FILTER(WHERE prepared AND featured AND detail AND copy),
          count(*) FILTER(WHERE copy AND NOT detail) FROM cohort""",
        (lower, upper),
    ).fetchone()
    steps = []
    previous = None
    for index, name in enumerate(("本期采集", "素材就绪", "首页展示", "客户打开详情", "获取文案")):
        count = int(counts[index])
        steps.append(
            {"name": name, "count": count, "conversion": count / previous if previous else None}
        )
        previous = count
    revenue = conn.execute(
        """SELECT service,count(*),COALESCE(sum(revenue_fen),0),
          count(*) FILTER(WHERE revenue_fen IS NULL)
          FROM billing_operations WHERE service IN ('viral_search','viral_detail','viral_copy')
          AND user_id IS NOT NULL AND state='SUCCEEDED'
          AND completed_at>=%s AND completed_at<%s GROUP BY service ORDER BY service""",
        (lower, upper),
    ).fetchall()
    costs = conn.execute(
        """SELECT COALESCE(sum(a.effective_cost_fen),0),
          count(*) FILTER(WHERE a.effective_cost_fen IS NULL) FROM billing_effective_attempts a
          JOIN billing_operations o ON o.id=a.operation_id
          WHERE a.created_at>=%s AND a.created_at<%s AND
          (o.service IN ('viral_search','viral_detail','viral_copy')
            OR (o.service='viral_data' AND o.user_id IS NULL))""",
        (lower, upper),
    ).fetchone()
    missing = conn.execute(
        """SELECT count(*) FROM billing_operations o
          WHERE o.service='viral_data' AND o.user_id IS NULL
          AND o.state IN ('SUCCEEDED','FAILED') AND COALESCE(o.completed_at,o.created_at)>=%s
          AND COALESCE(o.completed_at,o.created_at)<%s AND NOT EXISTS
          (SELECT 1 FROM billing_attempts a WHERE a.operation_id=o.id)""",
        (lower, upper),
    ).fetchone()[0]
    known_revenue = sum(float(row[2]) for row in revenue)
    unknown_revenue = sum(int(row[3]) for row in revenue)
    known_cost = float(costs[0])
    unknown_cost = int(costs[1]) + int(missing)
    top = conn.execute(
        """SELECT e.platform,e.video_id,v.title,count(*) AS requests
          FROM viral_content_usage_events e LEFT JOIN viral_videos v
          ON v.platform=e.platform AND v.video_id=e.video_id
          WHERE e.created_at>=%s AND e.created_at<%s GROUP BY e.platform,e.video_id,v.title
          ORDER BY count(*) DESC,e.platform,e.video_id LIMIT 10""",
        (lower, upper),
    ).fetchall()
    keywords = conn.execute(
        """SELECT keyword,platform,count(DISTINCT video_id) AS collected,
          count(DISTINCT video_id) FILTER(WHERE detail_at IS NOT NULL) AS used
          FROM viral_content_sources WHERE collected_at>=%s AND collected_at<%s
          GROUP BY keyword,platform ORDER BY
            count(DISTINCT video_id) FILTER(WHERE detail_at IS NOT NULL)::numeric
              /NULLIF(count(DISTINCT video_id),0) DESC,keyword,platform""",
        (lower, upper),
    ).fetchall()
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "measurementStartedAt": str(started),
        "cohortRule": (
            "同一区间实际采集的视频按平台和视频去重，后续阶段只跟踪这一批；"
            "逐步交集转化，不混入全部库存。"
        ),
        "historyNote": "采集来源和实际读取从起计时点记录；更早的批次、读取次数无法还原。",
        "funnel": steps,
        "directCopyWithoutDetail": int(counts[5]),
        "finance": {
            "knownRevenueFen": known_revenue,
            "unknownRevenueCount": unknown_revenue,
            "revenueFen": None if unknown_revenue else known_revenue,
            "knownCostFen": known_cost,
            "unknownCostCount": unknown_cost,
            "recordedCostFen": None if unknown_cost else known_cost,
            "costCoverageComplete": False,
            "costFen": None,
            "knownLedgerGrossFen": known_revenue - known_cost,
            "grossFen": None,
            "grossRate": None,
            "services": [
                {
                    "service": str(r[0]),
                    "operations": int(r[1]),
                    "knownRevenueFen": float(r[2]),
                    "unknownRevenueCount": int(r[3]),
                }
                for r in revenue
            ],
            "countingRule": (
                "收入为区间成功搜索、详情、文案台账；成本为区间对应业务及平台爆款数据接口的有效调用成本，"
                "包含失败调用。不按视频数量分摊。历史平台调用未能区分采集/准备/互动，分类无法还原。"
                "素材准备及存储的成本归属覆盖尚未补齐，完整成本和业务毛利保持待核对；已确认金额仍单列。"
            ),
        },
        "topVideos": [dict(r) for r in top],
        "bestKeywords": [dict(r) for r in keywords[:5]],
        "worstKeywords": [dict(r) for r in list(reversed(keywords))[:5]],
        "keywordRule": "按本期采集视频获得客户详情读取的比例排序；仅含起计以来真实采集来源。",
    }
