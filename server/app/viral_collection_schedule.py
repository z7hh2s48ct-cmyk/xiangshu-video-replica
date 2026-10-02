"""Explicit calendar schedules and read-only bounds for manual search costs."""

import hashlib
import json
from datetime import UTC, datetime, time, timedelta

from app.admin_dates import SHANGHAI
from app.billing_catalog import read_tariff
from app.db_portable import BusinessConnection
from app.viral_collection_billing import FrozenCollectionBilling, freeze_collection_billing
from app.viral_collection_budget import collection_budget
from app.viral_keywords import ViralKeywordConfig
from app.viral_tikhub import WECHAT_SEARCH_PAGES


def next_collection_time(now: datetime, interval_days: int, clock: time) -> datetime:
    local = now.astimezone(SHANGHAI)
    candidate = datetime.combine(local.date(), clock, SHANGHAI)
    if candidate <= local:
        candidate += timedelta(days=interval_days)
    return candidate.astimezone(UTC)


def advance_collection_time(
    now: datetime, interval_days: int, clock: time, previous: datetime | None
) -> datetime:
    """Skip missed periods after one catch-up run, preserving the saved weekday."""
    if previous is None:
        return next_collection_time(now, interval_days, clock)
    local = now.astimezone(SHANGHAI)
    anchor = datetime.combine(previous.astimezone(SHANGHAI).date(), clock, SHANGHAI)
    periods = max(0, (local.date() - anchor.date()).days // interval_days)
    candidate = anchor + timedelta(days=periods * interval_days)
    if candidate <= local:
        candidate += timedelta(days=interval_days)
    return candidate.astimezone(UTC)


def collection_estimate(
    conn: BusinessConnection, *, frozen_billing: FrozenCollectionBilling | None = None
) -> dict[str, object]:
    row = conn.execute("SELECT * FROM viral_runtime_controls WHERE id=1").fetchone()
    entries = (
        [
            ViralKeywordConfig.model_validate(item)
            for item in json.loads(row["keywords_json"] or "[]")
        ]
        if row
        else []
    )
    entries = [entry for entry in entries if entry.enabled]
    minimum = len(entries)
    maximum = sum(
        WECHAT_SEARCH_PAGES if entry.platform == "wechat_channels" else 1 for entry in entries
    )
    tariff = read_tariff(conn, "viral_data")
    cost = tariff.unit_cost_fen if tariff else None
    quota = sum(
        entry.limit if entry.limit is not None else int(row["per_keyword_limit"])
        for entry in entries
    )
    details = sum(
        entry.limit if entry.limit is not None else int(row["per_keyword_limit"])
        for entry in entries
        if entry.platform == "wechat_channels"
    )
    # Source requests permit at most three physical attempts and collection tasks
    # permit the initial run plus two checkpointed retries. Cached successes reduce
    # this ceiling; downloading and storage have separate, unpriced costs.
    physical_maximum = (maximum + details) * 3 * 3
    frozen = frozen_billing if frozen_billing is not None else freeze_collection_billing(conn)
    recipients = frozen.user_ids
    pricing = json.loads(frozen.pricing_json)
    snapshot = (
        {
            key: row[key]
            for key in (
                "keywords_json",
                "per_keyword_limit",
                "collection_enabled",
                "quality_min_likes",
                "quality_duration_min_ms",
                "quality_duration_max_ms",
                "quality_exclude_words_json",
                "monthly_budget_fen",
            )
        }
        if row
        else {}
    )
    snapshot.update(
        supplier_unit_cost_fen=str(cost) if cost is not None else None,
        recipients=recipients,
        pricing=pricing,
    )
    return {
        "enabledKeywords": len(entries),
        "searchCallsMin": minimum,
        "searchCallsMax": maximum,
        "searchCostMinFen": float(cost * minimum) if cost is not None else None,
        "searchCostMaxFen": float(cost * maximum) if cost is not None else None,
        "videoLimit": quota,
        "detailCallsMin": 0,
        "detailCallsMax": details,
        "physicalDataCallsMin": minimum,
        "physicalDataCallsMax": None,
        "normalRetryPhysicalCallsMax": physical_maximum,
        "dataCostMinFen": float(cost * minimum) if cost is not None else None,
        "dataCostMaxFen": None,
        "normalRetryDataCostMaxFen": float(cost * physical_maximum) if cost is not None else None,
        "mediaDownloadsMin": 0,
        "mediaDownloadsMax": quota * 3,
        "coverDownloadsMin": 0,
        "coverDownloadsMax": quota * 3,
        "storageCostFen": None,
        "transferCostFen": None,
        "cacheHits": None,
        "customerCount": len(recipients),
        "customerCreditsPerConfirmedCall": int(str(pricing["credits"])),
        "customerCreditsMaxEach": None,
        "normalRetryCustomerCreditsMaxEach": physical_maximum * int(str(pricing["credits"])),
        "snapshot": hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        "budget": collection_budget(conn),
        "totalCostFen": None,
        "note": (
            "搜索为单轮正常分页范围；视频号详情正常0至每词上限，缓存命中数量未知。"
            "常规重试估算含每次最多3次物理请求及本轮最多2次失败续跑；"
            "断机租约恢复可能产生更多未完成请求，最终调用和收费上限未知。"
            "去重、过滤、成功检查点和缓存复用会降低实际调用。下载按逻辑任务估算，不含HTTP重定向。"
            "费用按当前接口成本单价计算，是区间而非供应商承诺。下载流量和存储未接入计量，"
            "总费用未知，采集后以真实计量和财务核对为准。"
            "本操作会按既有规则向批次内符合条件客户收费：每次成功数据请求自动结算客户积分，"
            "不按入库视频数收费；余额不足、结算时停用客户不补扣。"
        ),
    }
