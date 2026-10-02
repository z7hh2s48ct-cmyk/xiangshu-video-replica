"""Atomic keyword operations and measured performance; legacy history stays unknown."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import ConfigDict, Field

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_dates import SHANGHAI
from app.admin_write_contract import AdminWriteContract, http_error, write_with_idempotency
from app.billing_reports import date_bounds
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.viral_keywords import ViralKeywordConfig, configured_viral_categories

router = APIRouter(prefix="/viral/keywords")


def keyword_rows(conn: psycopg.Connection, *, lock: bool = False) -> list[ViralKeywordConfig]:
    row = conn.execute(
        "SELECT keywords_json FROM viral_runtime_controls WHERE id=1"
        + (" FOR UPDATE" if lock else "")
    ).fetchone()
    return (
        [ViralKeywordConfig.model_validate(item) for item in json.loads(row[0] or "[]")]
        if row
        else []
    )


def keyword_performance(conn: BusinessConnection) -> dict[tuple[str, str], dict[str, Any]]:
    end = datetime.now(SHANGHAI).date()
    lower, upper = date_bounds(end - timedelta(days=29), end)
    rows = conn.execute(
        """WITH cohort AS (
            SELECT platform,keyword,video_id,bool_or(homepage_at IS NOT NULL) AS featured
            FROM viral_content_sources WHERE collected_at>=%s AND collected_at<%s
            GROUP BY platform,keyword,video_id
        ), reads AS (
            SELECT platform,video_id,count(*) AS uses,
                count(*) FILTER(WHERE kind='detail') AS details,
                count(*) FILTER(WHERE kind='copy') AS copies
            FROM viral_content_usage_events WHERE created_at>=%s AND created_at<%s
            GROUP BY platform,video_id
        ) SELECT c.platform,c.keyword,count(*) AS collected,
            count(*) FILTER(WHERE c.featured) AS featured,
            COALESCE(sum(r.uses),0)::bigint AS uses,
            COALESCE(sum(r.details),0)::bigint AS details,
            COALESCE(sum(r.copies),0)::bigint AS copies
        FROM cohort c LEFT JOIN reads r ON r.platform=c.platform AND r.video_id=c.video_id
        GROUP BY c.platform,c.keyword""",
        (lower, upper, lower, upper),
    ).fetchall()
    return {(str(row["platform"]), str(row["keyword"])): dict(row) for row in rows}


@router.get("")
def read_keywords(_actor: AdminReader) -> dict[str, object]:
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        configs = keyword_rows(raw)
        metrics = keyword_performance(conn)
        started = conn.execute(
            "SELECT started_at FROM viral_content_measurement_state WHERE id=1"
        ).fetchone()[0]
        end = datetime.now(SHANGHAI).date()
        lower, _ = date_bounds(end - timedelta(days=29), end)
        complete = started <= lower
        recent = conn.execute(
            "SELECT DISTINCT ON(platform,keyword) platform,keyword,status,started_at,finished_at "
            "FROM viral_keyword_runs ORDER BY platform,keyword,started_at DESC,batch_id DESC"
        ).fetchall()
        runs = {(str(row["platform"]), str(row["keyword"])): dict(row) for row in recent}
        items = []
        for config in configs:
            identity = (config.platform, config.keyword)
            metric = metrics.get(identity, {})
            collected, uses = int(metric.get("collected", 0)), int(metric.get("uses", 0))
            effect = (
                "unknown"
                if not complete or not collected
                else "high"
                if uses >= collected
                else "medium"
                if uses
                else "low"
            )
            items.append(
                {
                    **config.model_dump(),
                    "collected": collected,
                    "featured": int(metric.get("featured", 0)),
                    "uses": uses,
                    "details": int(metric.get("details", 0)),
                    "copies": int(metric.get("copies", 0)),
                    "effect": effect,
                    "lastRun": runs.get(identity),
                }
            )
        return {
            "items": items,
            "categories": configured_viral_categories(conn),
            "from": (end - timedelta(days=29)).isoformat(),
            "to": end.isoformat(),
            "measurementStartedAt": started.isoformat(),
            "coverageComplete": complete,
            "rule": (
                "近30天采集的视频去重；上首页追踪该批视频的实际展示；"
                "使用为该批视频在区间内成功打开详情和提取文案次数。"
                "多关键词关联的视频分别归因，不把词条数据相加为总计。"
            ),
            "effectRule": (
                "完整30天记录下，每条采集视频平均使用至少1次为高，有使用为中，无使用为低；"
                "无采集或历史覆盖不足为未知。"
            ),
        }


class KeywordEdit(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    expected: ViralKeywordConfig
    replacement: ViralKeywordConfig


class KeywordBatch(AdminWriteContract):
    model_config = ConfigDict(extra="forbid")
    keywords: list[ViralKeywordConfig] = Field(min_length=1, max_length=50)


def save_rows(
    raw: psycopg.Connection,
    rows: list[ViralKeywordConfig],
    actor: Any,
    reason: str,
    request_id: str,
    *,
    action: str,
    change: object,
) -> None:
    raw.execute(
        "UPDATE viral_runtime_controls SET keywords_json=%s,updated_by_user_id=%s,"
        "updated_at=now() WHERE id=1",
        (json.dumps([row.model_dump() for row in rows], ensure_ascii=False), actor.user_id),
    )
    raw.execute(
        "INSERT INTO audit_logs(id,actor_user_id,action,entity_type,entity_id,metadata_json) "
        "VALUES(%s,%s,%s,'viral_keyword','keywords',%s)",
        (
            str(uuid4()),
            actor.user_id,
            action,
            json.dumps(
                {"reason": reason, "request_id": request_id, "change": change}, ensure_ascii=False
            ),
        ),
    )


@router.patch("")
def edit_keyword(
    payload: KeywordEdit, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        rows = keyword_rows(raw, lock=True)
        target = next(
            (
                i
                for i, row in enumerate(rows)
                if (row.platform, row.keyword)
                == (payload.expected.platform, payload.expected.keyword)
            ),
            None,
        )
        if target is None or rows[target] != payload.expected:
            raise http_error(
                409, "VIRAL_KEYWORD_CONFLICT", "该关键词已被其他管理员修改或删除，请刷新后重试。"
            )
        replacement = payload.replacement
        categories = configured_viral_categories(BusinessConnection.postgres(raw))
        if replacement.category not in categories:
            raise http_error(422, "VIRAL_CATEGORY_INVALID", "请选择客户端分类目录中的分类。")
        if any(
            i != target
            and (row.platform, row.keyword) == (replacement.platform, replacement.keyword)
            for i, row in enumerate(rows)
        ):
            raise http_error(409, "VIRAL_KEYWORD_EXISTS", "该平台已有此关键词。")
        rows[target] = replacement
        save_rows(
            raw,
            rows,
            actor,
            payload.reason,
            request_id,
            action="viral_runtime.keyword_edit",
            change={"before": payload.expected.model_dump(), "after": replacement.model_dump()},
        )
        return {"updated": True, "item": replacement.model_dump()}

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="RUNTIME_SETTINGS_SERVICE_UNAVAILABLE",
        unavailable_message="关键词更新暂不可用。",
    )


@router.post("/batch")
def add_keywords(
    payload: KeywordBatch, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    def business(raw: psycopg.Connection, request_id: str) -> dict[str, object]:
        # Create and lock the singleton even before the first keyword is configured.
        raw.execute("INSERT INTO viral_runtime_controls(id) VALUES(1) ON CONFLICT DO NOTHING")
        rows = keyword_rows(raw, lock=True)
        categories = configured_viral_categories(BusinessConnection.postgres(raw))
        identities = {(row.platform, row.keyword) for row in rows}
        added = []
        for row in payload.keywords:
            if row.category not in categories:
                raise http_error(422, "VIRAL_CATEGORY_INVALID", "请选择客户端分类目录中的分类。")
            if (row.platform, row.keyword) not in identities:
                added.append(row)
                identities.add((row.platform, row.keyword))
        if len(rows) + len(added) > 20:
            raise http_error(409, "VIRAL_KEYWORD_LIMIT", "采集关键词最多20个，请先删除不需要的。")
        if added:
            save_rows(
                raw,
                rows + added,
                actor,
                payload.reason,
                request_id,
                action="viral_runtime.keyword_batch_add",
                change=[row.model_dump() for row in added],
            )
        return {
            "added": len(added),
            "duplicates": len(payload.keywords) - len(added),
            "total": len(rows) + len(added),
        }

    return write_with_idempotency(
        request,
        response,
        actor,
        payload,
        business,
        success_status=200,
        unavailable_code="RUNTIME_SETTINGS_SERVICE_UNAVAILABLE",
        unavailable_message="关键词新增暂不可用。",
    )
