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
from typing import Literal

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


class SummaryExportRequest(BaseModel):
    kind: Literal["business", "funds"]
    start_date: date
    end_date: date


@router.post("/reports/summary/export")
def export_summary(request: SummaryExportRequest, actor: AdminWriter) -> Response:
    """直接复用看板事实与日界；未知金额留中文标识，避免导出变成零。"""
    from app.admin_dashboard_routes import business_overview, funds_summary

    if request.end_date < request.start_date or (request.end_date - request.start_date).days > 366:
        raise HTTPException(422, detail="报表区间应为有效日期且最多 366 天")
    report = (
        business_overview(request.start_date, request.end_date, actor)
        if request.kind == "business"
        else funds_summary(request.start_date, request.end_date, actor)
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["区间开始", report["start"], "区间结束", report["end"], "时区", "北京时间"])
    writer.writerow(["说明", "金额单位为分；预收余额与对账异常为导出时点数，非历史月末快照"])

    def write(label: str, value: object) -> None:
        writer.writerow(
            [
                spreadsheet_safe_cell(label),
                "待核对" if value is None else spreadsheet_safe_cell(value),
            ]
        )

    labels = {
        "recharge_fen": "充值实收（分）",
        "revenue_fen": "已知确认收入（分）",
        "cost_fen": "已知成本（分）",
        "gross_fen": "毛利（分）",
        "margin_pct": "毛利率（%）",
        "paying_customers": "付费客户数",
        "new_paying_customers": "新增付费客户",
        "unknown_cost_count": "成本待核对数",
        "unknown_revenue_count": "收入待核对数",
        "pending_count": "待结算数",
        "legacy_cost_count": "历史未关联成本记录数",
        "legacy_settlement_count": "历史未关联结算记录数",
        "prepaid_credits": "当前预收积分余额",
        "prepaid_fen": "当前预收折合金额（分）",
        "orders": "实付订单数",
        "unverified_manual_orders": "历史人工订单待核对数（未计实收）",
        "unverified_manual_fen": "历史人工订单记录金额（待核对，分）",
        "offline_fen": "线下收款（分）",
        "grant_credits": "赠送积分",
        "refund_credits": "退款扣减积分",
        "refund_fen": "退款折合金额（分）",
        "net_fen": "净收入（分）",
        "reconciliation_problems": "当前对账异常数",
    }
    metrics = report["metrics"] if request.kind == "business" else report
    for field, label in labels.items():
        if field in metrics:
            write(label, metrics[field])
    if request.kind == "business":
        write(
            "客单价（分）",
            (
                metrics["recharge_fen"] / metrics["paying_customers"]
                if metrics["paying_customers"]
                else None
            ),
        )
        writer.writerow(["按日趋势", "已知确认收入（分）", "已知成本（分）", "毛利率（%）"])
        for item in report["daily"]:
            writer.writerow(
                [
                    item["day"],
                    item["known_revenue_fen"],
                    item["known_cost_fen"],
                    "待核对" if item["margin_pct"] is None else item["margin_pct"],
                ]
            )
        writer.writerow(["业务构成", "已知确认收入（分）", "已知成本（分）", "待核对成本数"])
        for item in report["modules"]:
            writer.writerow(
                [
                    spreadsheet_safe_cell(item["label"]),
                    item["revenue_fen"],
                    item["cost_fen"],
                    item["unknown_cost_count"],
                ]
            )
        writer.writerow(["客户编号", "客户名称", "用户名", "确认收入（分）"])
        for item in report["top_customers"]:
            writer.writerow(
                [
                    spreadsheet_safe_cell(item[k])
                    for k in ("user_id", "display_name", "username", "revenue_fen")
                ]
            )
    else:
        writer.writerow(["支付方式", "实付订单数", "金额（分）"])
        names = {
            "alipay": "支付宝",
            "wxpay": "微信",
            "offline": "线下转账",
            "unknown": "支付方式未知",
        }
        for item in report["by_method"]:
            writer.writerow([names[item["method"]], item["orders"], item["amount_fen"]])
    logger.info(
        "summary export: actor=%s kind=%s range=%s..%s",
        actor.username,
        request.kind,
        request.start_date,
        request.end_date,
    )
    filename = f"{request.kind}_{request.start_date}_{request.end_date}.csv"
    return Response(
        output.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )
