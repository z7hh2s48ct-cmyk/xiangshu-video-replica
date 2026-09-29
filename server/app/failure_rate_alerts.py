"""失败率告警（方案 P1-5）：按任务类型与错误码统计窗口内失败率。

方案原文要求「同一原因短时间大量出现」时，管理端能替技术负责人把
「个别客户的问题」和「整体故障」分开：本模块给「通知与告警」页提供
单一数据源——类型级失败率 + 类型内 Top 错误码（带 runbook 的分类 /
处理人 / 建议，与生成记录同一份词典）。

统计口径与「生成记录」列表一致：只数终局行（SUCCEEDED / FAILED /
SUBMISSION_UNCERTAIN / ARCHIVE_FAILED / UNKNOWN），窗口按 ``updated_at``
（最后一次状态变化）落在窗口内（默认近 1 小时）。客户取消不进分母——把「客户改
主意」算成故障会污染告警；进行中更不算——长任务会持续稀释失败率。

窗口 / 阈值 / 最小样本与接收人现读 ``alert_settings`` 单行表（P2-4 落地，
P1-5 写的「先做模块常量、配置落地后改读」已兑现）：报告与设置页读同一
行配置，模块常量退为设置行缺失时的兜底默认。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from app.admin_auth_routes import AdminReader, AdminWriter
from app.admin_dates import utc_timestamp_sql
from app.admin_write_contract import AdminWriteContract as AdminWriteRequest
from app.admin_write_contract import http_error as _http
from app.admin_write_contract import write_with_idempotency
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.failure_runbook import failure_explanation

router = APIRouter(prefix="/api/control", tags=["admin-alerts"])
logger = logging.getLogger(__name__)

FAILURE_RATE_WINDOW_MINUTES = 60
FAILURE_RATE_THRESHOLD_PERCENT = 30.0
# 小样本保护：近 1 小时只有个位数任务时，1/2 失败 = 50% 也不能算「突增」，
# 否则夜间低峰期会被单条失败刷屏。
FAILURE_RATE_MIN_SAMPLE = 5

# 接收人候选 = 管理端两种角色；设置页接收人下拉与更新校验共用这份定义。
_RECIPIENT_ROLES = ("admin", "auditor")
# 设置端点 fail-closed 的兜底文案（写侧由 write_with_idempotency 使用）。
_UNAVAILABLE_CODE = "ALERT_SETTINGS_SERVICE_UNAVAILABLE"
_UNAVAILABLE_MESSAGE = "告警设置需要 PostgreSQL 运行时。"

# 展示上限：每类任务最多列出几个错误码——告警页是「一眼看出」，不是账本。
_TOP_ERROR_LIMIT = 5

_SUCCEEDED_STATUSES = ("SUCCEEDED",)
_FAILED_STATUSES = ("FAILED", "SUBMISSION_UNCERTAIN", "ARCHIVE_FAILED", "UNKNOWN")
_COUNTED_STATUSES = _SUCCEEDED_STATUSES + _FAILED_STATUSES

# 口播表没有 error_code 列（只有原始 error_message）：展示码按状态映射，
# 与记录组装处（control_routes.oral_error_codes）同一口径。
_ORAL_ERROR_CODE_CASE = """(CASE task.status
                WHEN 'SUBMISSION_UNCERTAIN' THEN 'ORAL_SUBMISSION_UNCERTAIN'
                WHEN 'ARCHIVE_FAILED' THEN 'ORAL_ARCHIVE_FAILED'
                WHEN 'FAILED' THEN 'ORAL_TASK_FAILED'
                ELSE NULL
            END)"""

# 取帧与语义质量打分共表：沿用记录列表的审计判定分开统计——两类失败的
# 处理方向不同（重新取帧 vs 重新打分），合并计数会掩盖单边故障。
_SOURCE_FRAME_TYPE_CASE = """(CASE WHEN EXISTS (
                    SELECT 1 FROM audit_logs AS quality_audit
                    WHERE quality_audit.action = 'source_frame.semantic_quality_started'
                      AND quality_audit.entity_id = task.id
                ) THEN 'SOURCE_FRAME_AI_SCORE' ELSE 'SOURCE_FRAME_PROCESS' END)"""

# (record_type 表达式, 源表, error_code 表达式)；顺序即 UNION 顺序。
_BRANCHES: tuple[tuple[str, str, str], ...] = (
    ("'VIDEO'", "generation_tasks", "task.error_code"),
    ("'ORAL_VIDEO'", "oral_tasks", _ORAL_ERROR_CODE_CASE),
    ("'FIRST_FRAME_IMAGE'", "first_frame_tasks", "task.error_code"),
    ("'CHARACTER_SHEET_IMAGE'", "character_sheet_tasks", "task.error_code"),
    ("'CHARACTER_VIEW_IMAGE'", "character_generation_tasks", "task.error_code"),
    (_SOURCE_FRAME_TYPE_CASE, "source_frame_tasks", "task.error_code"),
    ("'ANALYSIS'", "analysis_tasks", "task.error_code"),
)


class FailureRateError(BaseModel):
    """类型内单个错误码的失败计数；分类/处理人是稳定代码（标签由前端翻译），建议来自 runbook。"""

    model_config = ConfigDict(extra="forbid")

    error_code: str | None
    count: int
    category: str | None
    owner: str | None
    advice: str | None


class FailureRateGroup(BaseModel):
    """单类任务的窗口内失败率与主要错误码。"""

    model_config = ConfigDict(extra="forbid")

    record_type: str
    total: int
    failed: int
    failure_rate_percent: float
    exceeded: bool
    top_errors: list[FailureRateError]


class FailureRateReport(BaseModel):
    """「通知与告警」页的失败率报告；``alerting`` 为真时提醒技术负责人。"""

    model_config = ConfigDict(extra="forbid")

    generated_at: str
    window_minutes: int
    threshold_percent: float
    min_sample_size: int
    recipient_user_id: str | None
    recipient_display_name: str | None
    total: int
    failed: int
    failure_rate_percent: float
    exceeded: bool
    alerting: bool
    groups: list[FailureRateGroup]


@dataclass(frozen=True)
class FailureRateSettings:
    """失败率告警口径 + 接收人（``alert_settings`` 单行表的读取结果）。"""

    window_minutes: int
    threshold_percent: float
    min_sample_size: int
    recipient_user_id: str | None
    recipient_display_name: str | None


def _failure_rate_percent(failed: int, total: int) -> float:
    return round(failed * 100 / total, 1) if total > 0 else 0.0


def _exceeds_threshold(
    failed: int, total: int, *, threshold_percent: float, min_sample_size: int
) -> bool:
    if failed <= 0 or total < min_sample_size:
        return False
    return failed * 100 / total >= threshold_percent


def load_failure_rate_settings(conn: BusinessConnection) -> FailureRateSettings:
    """读取单行设置；行缺失时兜底模块常量（只可能出现在手工清库的瞬间）。

    报告宁可继续按默认口径服务，也不因配置行缺失整体不可用——与未知
    错误码 fail-open 的展示口径一致。
    """
    row = conn.execute(
        "SELECT s.failure_rate_window_minutes, s.failure_rate_threshold_percent, "
        "       s.failure_rate_min_sample, s.recipient_user_id, u.display_name "
        "FROM alert_settings AS s "
        "LEFT JOIN users AS u ON u.id = s.recipient_user_id "
        "WHERE s.id = 1"
    ).fetchone()
    if row is None:
        return FailureRateSettings(
            window_minutes=FAILURE_RATE_WINDOW_MINUTES,
            threshold_percent=FAILURE_RATE_THRESHOLD_PERCENT,
            min_sample_size=FAILURE_RATE_MIN_SAMPLE,
            recipient_user_id=None,
            recipient_display_name=None,
        )
    return FailureRateSettings(
        window_minutes=int(row[0]),
        threshold_percent=float(row[1]),
        min_sample_size=int(row[2]),
        recipient_user_id=None if row[3] is None else str(row[3]),
        recipient_display_name=None if row[4] is None else str(row[4]),
    )


def build_failure_rate_report(
    conn: BusinessConnection,
    *,
    now: datetime | None = None,
    settings: FailureRateSettings | None = None,
) -> FailureRateReport:
    """窗口内各类型任务的终局失败率；口径读 ``alert_settings``（两个参数仅测试注入）。"""
    moment = now.astimezone(UTC) if now else datetime.now(UTC)
    effective = settings or load_failure_rate_settings(conn)
    cutoff = (moment - timedelta(minutes=effective.window_minutes)).isoformat()
    timestamp_sql = utc_timestamp_sql("task.updated_at", postgres=conn.is_postgres)
    window_parameter = "%s::timestamptz" if conn.is_postgres else "datetime(%s)"
    status_placeholders = ", ".join(["%s"] * len(_COUNTED_STATUSES))
    union_sql = "\n            UNION ALL\n".join(
        f"""SELECT {record_type} AS record_type, task.status AS status,
                   {error_code} AS error_code
            FROM {table} AS task
            WHERE {timestamp_sql} >= {window_parameter}
              AND task.status IN ({status_placeholders})"""
        for record_type, table, error_code in _BRANCHES
    )
    params: list[Any] = []
    for _ in _BRANCHES:
        params.append(cutoff)
        params.extend(_COUNTED_STATUSES)
    rows = conn.execute(
        f"""
        SELECT record_type, status, error_code, COUNT(*) AS total
        FROM (
            {union_sql}
        ) AS terminal_rows
        GROUP BY record_type, status, error_code
        """,  # noqa: S608
        tuple(params),
    ).fetchall()

    totals: dict[str, int] = defaultdict(int)
    failed_by_type: dict[str, int] = defaultdict(int)
    error_counts: dict[str, dict[str | None, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        record_type = str(row["record_type"])
        count = int(row["total"])
        totals[record_type] += count
        if str(row["status"]) in _FAILED_STATUSES:
            failed_by_type[record_type] += count
            raw_code = row["error_code"]
            error_counts[record_type][str(raw_code) if raw_code is not None else None] += count

    groups: list[FailureRateGroup] = []
    for record_type, total in totals.items():
        failed = failed_by_type.get(record_type, 0)
        top_errors: list[FailureRateError] = []
        for error_code, count in sorted(
            error_counts.get(record_type, {}).items(),
            key=lambda item: (-item[1], item[0] or ""),
        )[:_TOP_ERROR_LIMIT]:
            # 内容审核升级依赖服务商原话；告警页没有原话样本，按错误码
            # 静态分类展示（原话级判定在生成记录详情里做）。
            explanation = failure_explanation(error_code)
            top_errors.append(
                FailureRateError(
                    error_code=error_code,
                    count=count,
                    category=explanation.category if explanation else None,
                    owner=explanation.owner if explanation else None,
                    advice=explanation.advice if explanation else None,
                )
            )
        groups.append(
            FailureRateGroup(
                record_type=record_type,
                total=total,
                failed=failed,
                failure_rate_percent=_failure_rate_percent(failed, total),
                exceeded=_exceeds_threshold(
                    failed,
                    total,
                    threshold_percent=effective.threshold_percent,
                    min_sample_size=effective.min_sample_size,
                ),
                top_errors=top_errors,
            )
        )
    # 超阈值的排最前（技术负责人先看到真问题），其余失败多的在前。
    groups.sort(
        key=lambda group: (not group.exceeded, -group.failed, -group.total, group.record_type)
    )

    total_all = sum(totals.values())
    failed_all = sum(failed_by_type.values())
    return FailureRateReport(
        generated_at=moment.isoformat(),
        window_minutes=effective.window_minutes,
        threshold_percent=effective.threshold_percent,
        min_sample_size=effective.min_sample_size,
        recipient_user_id=effective.recipient_user_id,
        recipient_display_name=effective.recipient_display_name,
        total=total_all,
        failed=failed_all,
        failure_rate_percent=_failure_rate_percent(failed_all, total_all),
        exceeded=_exceeds_threshold(
            failed_all,
            total_all,
            threshold_percent=effective.threshold_percent,
            min_sample_size=effective.min_sample_size,
        ),
        alerting=any(group.exceeded for group in groups),
        groups=groups,
    )


@router.get("/alerts/failure-rate", response_model=FailureRateReport)
def read_failure_rate_report(_actor: AdminReader) -> FailureRateReport:
    """失败率报告（方案 P1-5「通知与告警」页数据源）；口径读设置单行表。

    只读端点走 ``AdminReader``；管理会话本身要求 PG 运行时
    （admin_runtime_routes 同约定），因此这里直接用 pg_transaction。
    """
    with pg_transaction() as raw:
        return build_failure_rate_report(BusinessConnection.postgres(raw))


# ---------------------------------------------------------------------------
# 通知与告警设置（P2-4）：接收人与失败率口径的读写
# ---------------------------------------------------------------------------

_SETTINGS_SELECT = (
    "SELECT s.recipient_user_id, u.display_name, s.failure_rate_window_minutes, "
    "       s.failure_rate_threshold_percent, s.failure_rate_min_sample, "
    "       s.updated_by_user_id, s.updated_at "
    "FROM alert_settings AS s "
    "LEFT JOIN users AS u ON u.id = s.recipient_user_id "
    "WHERE s.id = 1"
)


class AlertSettingsSnapshot(BaseModel):
    """「通知与告警」当前设置：接收人（可空）+ 失败率口径 + 最近修改。"""

    model_config = ConfigDict(extra="forbid")

    recipient_user_id: str | None
    recipient_display_name: str | None
    failure_rate_window_minutes: int
    failure_rate_threshold_percent: float
    failure_rate_min_sample: int
    updated_by_user_id: str | None
    updated_at: str | None


class AlertSettingsUpdate(AdminWriteRequest):
    """全量更新告警设置：三个数值必填（范围与迁移 CHECK 一致）。

    ``recipient_user_id`` 空串 / 缺省 = 显式不指定接收人（「没人接收」是
    合法状态，不编造默认收件人）。
    """

    model_config = ConfigDict(extra="forbid")

    recipient_user_id: str | None = None
    failure_rate_window_minutes: int = Field(ge=1, le=10080)
    failure_rate_threshold_percent: float = Field(ge=0, le=100)
    failure_rate_min_sample: int = Field(ge=1)


def _iso_timestamp(value: object) -> str | None:
    """timestamptz 列 → ISO 文本；None 透传。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _load_alert_settings_snapshot(conn: BusinessConnection) -> AlertSettingsSnapshot:
    """设置页快照；行缺失 fail-closed——配置页不展示假状态（报告才兜底）。"""
    row = conn.execute(_SETTINGS_SELECT).fetchone()
    if row is None:
        raise _http(503, _UNAVAILABLE_CODE, _UNAVAILABLE_MESSAGE)
    return AlertSettingsSnapshot(
        recipient_user_id=None if row[0] is None else str(row[0]),
        recipient_display_name=None if row[1] is None else str(row[1]),
        failure_rate_window_minutes=int(row[2]),
        failure_rate_threshold_percent=float(row[3]),
        failure_rate_min_sample=int(row[4]),
        updated_by_user_id=None if row[5] is None else str(row[5]),
        updated_at=_iso_timestamp(row[6]),
    )


@router.get("/settings/alerts", response_model=AlertSettingsSnapshot)
def read_alert_settings(response: Response, _actor: AdminReader) -> AlertSettingsSnapshot:
    """告警设置快照（P2-4「通知与告警」页设置区块）。"""
    response.headers["Cache-Control"] = "no-store"
    with pg_transaction() as raw:
        return _load_alert_settings_snapshot(BusinessConnection.postgres(raw))


@router.put("/settings/alerts", response_model=AlertSettingsSnapshot)
def update_alert_settings(
    body: AlertSettingsUpdate, request: Request, response: Response, actor: AdminWriter
) -> dict[str, object]:
    """全量更新告警设置；接收人必须是启用中的管理员（admin / auditor）。"""

    def business(conn: psycopg.Connection, request_id: str) -> dict[str, object]:
        bridge = BusinessConnection.postgres(conn)
        recipient = (body.recipient_user_id or "").strip() or None
        if recipient is not None:
            target = conn.execute(
                "SELECT 1 FROM users WHERE id = %s AND role = ANY(%s) AND is_active = 1",
                (recipient, list(_RECIPIENT_ROLES)),
            ).fetchone()
            if target is None:
                raise _http(
                    400,
                    "ALERT_SETTINGS_VALIDATION_FAILED",
                    "接收人必须是启用中的管理员账号。",
                )
        before = _load_alert_settings_snapshot(bridge)
        conn.execute(
            "UPDATE alert_settings SET recipient_user_id = %s, "
            " failure_rate_threshold_percent = %s, failure_rate_window_minutes = %s, "
            " failure_rate_min_sample = %s, updated_by_user_id = %s, "
            " updated_at = clock_timestamp() WHERE id = 1",
            (
                recipient,
                body.failure_rate_threshold_percent,
                body.failure_rate_window_minutes,
                body.failure_rate_min_sample,
                actor.user_id,
            ),
        )
        after = _load_alert_settings_snapshot(bridge)
        conn.execute(
            "INSERT INTO audit_logs "
            "(id, actor_user_id, action, entity_type, entity_id, metadata_json) "
            "VALUES (%s, %s, 'alerts.settings.update', 'alert_settings', '1', %s)",
            (
                str(uuid.uuid4()),
                actor.user_id,
                json.dumps(
                    {
                        "old": before.model_dump(),
                        "new": after.model_dump(),
                        "reason": body.reason.strip(),
                        "request_id": request_id,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
        )
        logger.info(
            "alert settings updated: window=%s threshold=%s min_sample=%s recipient=%s "
            "actor=%s request=%s",
            after.failure_rate_window_minutes,
            after.failure_rate_threshold_percent,
            after.failure_rate_min_sample,
            after.recipient_user_id,
            actor.user_id,
            request_id,
        )
        # 响应契约是 AlertSettingsSnapshot（extra=forbid）：payload 不带
        # request_id（它只走 X-Request-Id 头），审计里仍记请求 id。
        return after.model_dump()

    return write_with_idempotency(
        request,
        response,
        actor,
        body,
        business,
        success_status=200,
        unavailable_code=_UNAVAILABLE_CODE,
        unavailable_message=_UNAVAILABLE_MESSAGE,
    )


@router.get("/settings/alerts/recipient-candidates")
def list_alert_recipient_candidates(actor: AdminReader) -> dict[str, object]:
    """可作接收人的账号：启用中的 admin / auditor（读侧，普通管理员可见）。"""
    del actor
    with pg_transaction() as conn:
        rows = conn.execute(
            "SELECT id, username, display_name, role FROM users "
            "WHERE role = ANY(%s) AND is_active = 1 ORDER BY username, id",
            (list(_RECIPIENT_ROLES),),
        ).fetchall()
    return {
        "items": [
            {
                "user_id": str(row[0]),
                "username": str(row[1]),
                "display_name": str(row[2]),
                "role": str(row[3]),
            }
            for row in rows
        ]
    }
