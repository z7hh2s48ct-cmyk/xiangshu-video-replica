"""Demand joins observed searches to currently customer-usable inventory."""

import json
from datetime import date, timedelta
from typing import Any

from app.admin_dates import SHANGHAI
from app.db_portable import BusinessConnection
from app.viral_content_state import customer_visible_sql
from app.viral_keywords import ViralKeywordConfig, configured_viral_categories


def enrich_demand(
    conn: BusinessConnection, rows: list[Any], first: str, last: str, started: Any
) -> tuple[list[dict[str, Any]], list[str]]:
    inventory = conn.execute(
        f"""WITH associations AS (
            SELECT platform,keyword,video_id FROM viral_content_sources
            UNION SELECT platform,keyword,video_id FROM viral_search_discoveries
        ) SELECT a.platform,a.keyword,count(DISTINCT a.video_id) AS inventory
        FROM associations a JOIN viral_videos v
            ON v.platform=a.platform AND v.video_id=a.video_id
        WHERE {customer_visible_sql("v")}
        GROUP BY a.platform,a.keyword"""
    ).fetchall()
    counts = {(row["platform"], row["keyword"]): int(row["inventory"]) for row in inventory}
    control = conn.execute("SELECT keywords_json FROM viral_runtime_controls WHERE id=1").fetchone()
    configs = (
        [ViralKeywordConfig.model_validate(item) for item in json.loads(control[0] or "[]")]
        if control
        else []
    )
    configured = {(row.platform, row.keyword): row for row in configs}
    trend = conn.execute(
        "SELECT platform,keyword,search_date,count(*) AS searches "
        "FROM viral_search_events WHERE search_date BETWEEN %s AND %s "
        "GROUP BY platform,keyword,search_date",
        (first, last),
    ).fetchall()
    daily = {
        (row["platform"], row["keyword"], str(row["search_date"])): int(row["searches"])
        for row in trend
    }
    first_day, last_day = date.fromisoformat(first), date.fromisoformat(last)
    marker = started.astimezone(SHANGHAI).date() if started else None
    result = []
    for row in rows:
        item = dict(row)
        key = (item["platform"], item["keyword"])
        config = configured.get(key)
        item["inventory"] = counts.get(key, 0)
        item["configured"] = config is not None
        item["collectionEnabled"] = config.enabled if config else None
        item["trend"] = []
        day = first_day
        while day <= last_day:
            recorded = marker is not None and day >= marker
            item["trend"].append(
                {
                    "date": day.isoformat(),
                    "searches": daily.get((*key, day.isoformat()), 0) if recorded else None,
                    "partial": marker == day,
                }
            )
            day += timedelta(days=1)
        result.append(item)
    # Observable ratio only; it does not imply an unmeasured amount of unmet demand.
    result.sort(
        key=lambda row: (
            -(row["searches"] or 0) / (row["inventory"] + 1),
            -(row["searches"] or 0),
            row["inventory"],
            row["keyword"],
            row["platform"],
        )
    )
    return result, configured_viral_categories(conn)
