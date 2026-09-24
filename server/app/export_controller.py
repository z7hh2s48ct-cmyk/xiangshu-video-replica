"""管理端计费报表导出（CSV）。

对齐项目管理端惯例：AdminReader 鉴权（app.admin_auth_routes）、/api/control 前缀、
pg_transaction + BusinessConnection。按 CLAUDE.md「无明确请求不引入新依赖」，
xlsx 依赖的 openpyxl 不在 server 依赖组内，故仅支持 CSV（gzip 压缩）。
数据模型无 customer email（见 admin_customer_routes.py §T33），导出以 username 标识用户。
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.admin_auth_routes import AdminWriter
from app.csv_export import spreadsheet_safe_cell
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/control", tags=["admin-export"])

_MAX_EXPORT_DAYS = 90
_SUPPORTED_FORMATS = ("csv",)


class ExportRequest(BaseModel):
    """报表导出参数。"""

    format: str = Field(default="csv", description="输出格式（当前仅支持 csv）")
    start_date: date = Field(..., description="起始日期（含）")
    end_date: date = Field(..., description="结束日期（含）")
    service_types: list[str] = Field(default=["all"], description="按服务过滤")
    user_ids: list[str] | None = Field(None, description="按用户过滤（None=全部）")
    min_revenue_fen: int | None = Field(None, ge=0, description="最小费用过滤")
    max_revenue_fen: int | None = Field(None, ge=0, description="最大费用过滤")


def validate_request(request: ExportRequest) -> tuple[bool, str]:
    """校验导出参数；返回 (是否合法, 不合法原因)。"""
    if request.start_date > request.end_date:
        return False, "start_date must be before or equal to end_date"
    if (request.end_date - request.start_date).days > _MAX_EXPORT_DAYS:
        return False, "Date range cannot exceed 90 days"
    if request.format not in _SUPPORTED_FORMATS:
        return False, f"Unsupported format: {request.format}"
    return True, ""


def generate_csv_content(conn: BusinessConnection, request: ExportRequest) -> bytes:
    """按过滤条件生成 gzip 压缩的 CSV 报表。

    费用过滤不能用 SELECT 别名（WHERE 阶段不可见），重复 cost 表达式。
    billing_tariffs 未配置的服务按 0 费用计（LEFT JOIN 产出 NULL）。
    """
    # 含 end_date 当天：右开区间到次日零点
    start_inclusive = request.start_date
    end_exclusive = request.end_date + timedelta(days=1)
    service_filter = [] if request.service_types == ["all"] else request.service_types

    rows = conn.execute(
        """
        SELECT
            wt.id,
            wt.user_id,
            u.username AS username,
            DATE(wt.created_at) AS billing_date,
            bo.service AS service_type,
            wt.available_delta AS credits_delta,
            ABS(wt.available_delta) AS credits_used,
            s.unit_cost_fen AS unit_cost_fen,
            ABS(wt.available_delta) * COALESCE(s.unit_cost_fen, 0) / 100.0 AS cost_fen,
            wt.billing_round
        FROM wallet_transactions wt
        JOIN billing_operations bo ON wt.billing_operation_id = bo.id
        LEFT JOIN billing_tariffs s ON s.service = bo.service
        LEFT JOIN users u ON wt.user_id = u.id
        WHERE wt.created_at >= %s AND wt.created_at < %s
          AND (%s::text[] IS NULL OR bo.service = ANY(%s::text[]))
          AND (%s::text[] IS NULL OR wt.user_id = ANY(%s::text[]))
          AND (%s IS NULL OR ABS(wt.available_delta) * COALESCE(s.unit_cost_fen, 0) / 100.0 >= %s)
          AND (%s IS NULL OR ABS(wt.available_delta) * COALESCE(s.unit_cost_fen, 0) / 100.0 <= %s)
        ORDER BY wt.created_at DESC, wt.id
        """,
        (
            start_inclusive,
            end_exclusive,
            service_filter or None,
            service_filter or None,
            request.user_ids,
            request.user_ids,
            request.min_revenue_fen,
            request.min_revenue_fen,
            request.max_revenue_fen,
            request.max_revenue_fen,
        ),
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "Transaction ID",
            "User ID",
            "Username",
            "Billing Date",
            "Service Type",
            "Credits Delta",
            "Credits Used",
            "Unit Cost (Fen)",
            "Total Cost (Fen)",
            "Billing Round",
        ]
    )
    for row in rows:
        # username 是客户可控输入，与其它导出 lane 一致走六前缀防护，防公式注入。
        writer.writerow(
            [
                spreadsheet_safe_cell(cell)
                for cell in (
                    str(row["id"]),
                    str(row["user_id"] or "N/A"),
                    row["username"] or "N/A",
                    row["billing_date"].isoformat(),
                    row["service_type"],
                    int(row["credits_delta"]),
                    int(row["credits_used"]),
                    float(row["unit_cost_fen"] or 0),
                    float(row["cost_fen"] or 0),
                    int(row["billing_round"]),
                )
            ]
        )
    return gzip.compress(output.getvalue().encode("utf-8"))


@router.post("/reports/export")
def export_statistics(request: ExportRequest, actor: AdminWriter) -> Response:
    """导出计费统计报表（CSV，gzip 压缩）。

    只读查询走 REPEATABLE READ 快照，导出行集与账务时点一致。
    """
    valid, message = validate_request(request)
    if not valid:
        raise HTTPException(status_code=400, detail=message)

    with pg_transaction(isolation="REPEATABLE READ") as raw:
        conn = BusinessConnection.postgres(raw)
        content = generate_csv_content(conn, request)

    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"billing_export_{timestamp}.csv.gz"
    logger.info(
        "billing export: actor=%s range=%s..%s format=%s",
        actor.username,
        request.start_date,
        request.end_date,
        request.format,
    )
    return Response(
        content=content,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
