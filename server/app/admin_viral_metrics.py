"""内容经营读模型：按持久台账的钱包主体匹配视频，未知金额不伪装为零。"""

from __future__ import annotations

from typing import Any

from app.db_portable import BusinessConnection
from app.viral_resource_metering import resource_records, resource_summary

# 结算流水保留当时的钱包主体，避免将子账号操作者当作收费主体。
# 无结算流水的历史/免费记录才回落当前母账号关系；从不截取含任意字符的视频ID。
WALLET_OWNER_SQL = """COALESCE(
    (SELECT wt.user_id FROM wallet_transactions wt
     WHERE wt.billing_operation_id=o.id AND wt.type='SETTLE'
     ORDER BY wt.created_at,wt.id LIMIT 1),
    (SELECT COALESCE(u.parent_user_id,u.id) FROM users u WHERE u.id=o.user_id),o.user_id)
"""
PURCHASE_VIDEO_MATCH_SQL = f"""o.source_id =
    (CASE o.service WHEN 'viral_detail' THEN 'viral-detail:' ELSE 'viral-copy:' END)
    || {WALLET_OWNER_SQL} || ':' || v.platform || ':' || v.video_id
"""


def usage_count_sql(service: str) -> str:
    if service not in {"viral_detail", "viral_copy"}:
        raise ValueError("不支持的内容授权业务")
    return (
        f"(SELECT count(DISTINCT {WALLET_OWNER_SQL}) FROM billing_operations o "
        f"WHERE o.service='{service}' AND o.state='SUCCEEDED' AND o.user_id IS NOT NULL "
        f"AND {PURCHASE_VIDEO_MATCH_SQL})"
    )


def video_business_metrics(
    conn: BusinessConnection, *, platform: str, video_id: str, offset: int = 0, limit: int = 20
) -> dict[str, object]:
    rows = conn.execute(
        f"""
        SELECT {WALLET_OWNER_SQL} AS wallet_owner_id,o.service,o.charged_credits,
               o.revenue_fen,o.completed_at,o.user_id AS actor_user_id,
               COALESCE(u.display_name,u.username) AS actor_name
        FROM billing_operations o JOIN viral_videos v ON v.platform=%s AND v.video_id=%s
        LEFT JOIN users u ON u.id=o.user_id
        WHERE o.service IN ('viral_detail','viral_copy') AND o.state='SUCCEEDED'
          AND o.user_id IS NOT NULL AND {PURCHASE_VIDEO_MATCH_SQL}
        ORDER BY o.completed_at DESC,o.id DESC
        """,
        (platform, video_id),
    ).fetchall()
    customers: dict[str, dict[str, Any]] = {}
    known_revenue = 0.0
    unknown_revenue = 0
    charged = 0
    for row in rows:
        owner = str(row["wallet_owner_id"])
        customer = customers.setdefault(
            owner,
            {
                "walletOwnerId": owner,
                "detailAuthorized": False,
                "copyAuthorized": False,
                "chargedCredits": 0,
                "knownRevenueFen": 0.0,
                "unknownRevenueOperations": 0,
                "lastUsedAt": str(row["completed_at"]),
                "actors": [],
            },
        )
        customer["detailAuthorized" if row["service"] == "viral_detail" else "copyAuthorized"] = (
            True
        )
        credits = int(row["charged_credits"])
        charged += credits
        customer["chargedCredits"] = int(customer["chargedCredits"]) + credits
        actors = customer["actors"]
        assert isinstance(actors, list)
        actor = {"id": str(row["actor_user_id"]), "name": row["actor_name"]}
        if actor not in actors:
            actors.append(actor)
        if row["revenue_fen"] is None:
            unknown_revenue += 1
            customer["unknownRevenueOperations"] = int(customer["unknownRevenueOperations"]) + 1
        else:
            revenue = float(row["revenue_fen"])
            known_revenue += revenue
            customer["knownRevenueFen"] = float(customer["knownRevenueFen"]) + revenue
    favorites = conn.execute(
        "SELECT count(*) FROM viral_video_favorites WHERE platform=%s AND video_id=%s",
        (platform, video_id),
    ).fetchone()
    for owner, customer in customers.items():
        name = conn.execute(
            "SELECT COALESCE(display_name,username) FROM users WHERE id=%s", (owner,)
        ).fetchone()
        customer["name"] = name[0] if name else "历史客户"
        customer["revenueFen"] = (
            None if customer["unknownRevenueOperations"] else customer["knownRevenueFen"]
        )
    costs = conn.execute(
        "SELECT count(*),COALESCE(sum(a.effective_cost_fen),0),"
        "count(*) FILTER(WHERE a.effective_cost_fen IS NULL AND a.state<>'PENDING'),"
        "count(*) FILTER(WHERE a.state='PENDING') "
        "FROM billing_operations o JOIN billing_effective_attempts a ON a.operation_id=o.id "
        "WHERE o.user_id IS NULL AND o.service='viral_data' "
        "AND o.api_metadata::jsonb->>'video_platform'=%s "
        "AND o.api_metadata::jsonb->>'video_id'=%s",
        (platform, video_id),
    ).fetchone()
    transcription = conn.execute(
        "SELECT count(*),COALESCE(sum(a.effective_cost_fen),0),"
        "count(*) FILTER(WHERE a.effective_cost_fen IS NULL) "
        "FROM billing_operations o JOIN billing_effective_attempts a ON a.operation_id=o.id "
        "JOIN script_from_audio_tasks t ON t.id=o.source_id WHERE o.service='asr' "
        "AND t.request_json::jsonb->'viral_source'->>0=%s "
        "AND t.request_json::jsonb->'viral_source'->>1=%s",
        (platform, video_id),
    ).fetchone()
    resources = resource_summary(resource_records(conn, platform=platform, video_id=video_id))
    resources["recordTotal"] = len(resources.pop("records"))
    return {
        "window": "全部已记录历史",
        "countingRule": (
            "详情和文案按成功授权台账的钱包主体去重；子账号共享母账号授权。"
            "收藏为当前有效收藏，不是点击次数。"
        ),
        "detailAccounts": sum(bool(c["detailAuthorized"]) for c in customers.values()),
        "copyAccounts": sum(bool(c["copyAuthorized"]) for c in customers.values()),
        "favoriteAccounts": int(favorites[0]),
        "chargedCredits": charged,
        "revenueFen": None if unknown_revenue else known_revenue,
        "knownRevenueFen": known_revenue,
        "unknownRevenueOperations": unknown_revenue,
        "collectionCostFen": None,
        "resources": resources,
        "transcriptionCalls": int(transcription[0]),
        "knownTranscriptionCostFen": float(transcription[1]),
        "unknownTranscriptionCostCalls": int(transcription[2]),
        "attributedDataCalls": int(costs[0]),
        "knownDataCostFen": float(costs[1]),
        "unknownDataCostCalls": int(costs[2]),
        "pendingDataCostCalls": int(costs[3]),
        "costNote": "仅统计上线后明确归属此视频的接口请求；共享搜索不按视频平摊。"
        "新素材准备、下载/转存、应用观察存储保留与后端读取有独立事件。"
        "历史与云端实际计价、直链流量仍缺证据，总成本未知。",
        "customers": list(customers.values())[offset : offset + limit],
        "customerTotal": len(customers),
        "offset": offset,
        "limit": limit,
    }
