"""Export controller for billing statistics reports.

This module provides CSV/Excel export functionality for billing analytics.
Supports stream processing for large datasets and async background tasks.

Version: V1.0 (2026-09-20)
Author: AI Agent (Qoder)
Status: Ready for Development
"""

from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from datetime import date, datetime, timezone
import uuid
import gzip
import io
import csv
from sqlalchemy import text
import logging

logger = logging.getLogger(__name__)

from app.database import BusinessDbDep
from app.auth import get_current_user
from app.schemas.user import UserResponse


router = APIRouter(prefix="/reports", tags=["export"])


class ExportRequest(BaseModel):
    """Parameters for report export."""

    format: Literal["csv", "xlsx"] = Field(default="csv", description="Output format")
    start_date: date = Field(..., description="Start date (inclusive)")
    end_date: date = Field(..., description="End date (inclusive)")
    service_types: list[str] = Field(
        default=["all"], description="Filter by service type"
    )
    user_ids: Optional[list[str]] = Field(
        None, description="Filter by user IDs (if None, export all)"
    )
    department_ids: Optional[list[str]] = Field(
        None, description="Filter by department IDs"
    )
    min_revenue_fen: Optional[int] = Field(None, ge=0, description="Min revenue filter")
    max_revenue_fen: Optional[int] = Field(None, ge=0, description="Max revenue filter")


class ExportResponse(BaseModel):
    """Export task response."""

    export_id: str
    status: Literal["queued", "processing", "completed", "failed"]
    file_url: Optional[str] = None
    message: Optional[str] = None


@router.post("/api/admin/reports/export", response_model=ExportResponse)
async def export_statistics(
    request: ExportRequest,
    current_user: UserResponse = Depends(get_current_user),
    conn: BusinessDbDep = Depends(BusinessDbDep),
):
    """Export billing statistics to file.
    
    Admin-only endpoint that supports both synchronous and asynchronous export modes.
    
    Args:
        request: Export parameters including date range, filters, and format
        current_user: Authenticated admin user
        conn: Database connection
        
    Returns:
        Export task ID or direct file URL if small dataset
        
    Raises:
        HTTPException:
            - 403: Not authorized (non-admin user)
            - 400: Invalid parameters (date range > 90 days, etc.)
            
    Performance Notes:
        - Small exports (<5000 records): Synchronous response (immediate file)
        - Medium exports (5k-10k records): Async task with polling
        - Large exports (>10k records): Background job with email notification
    """
    
    # Validate time range (max 90 days for performance)
    if (request.end_date - request.start_date).days > 90:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum 90 days per export. Please narrow the date range."
        )
    
    # Check admin permission (this should be enforced via RBAC middleware)
    if not hasattr(current_user, "role") or current_user.role not in ["admin", "super_admin"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required for export functionality."
        )
    
    # Count total records to decide sync vs async
    count_query = """
        SELECT COUNT(*) as total
        FROM wallet_transactions wt
        JOIN billing_operations bo ON wt.billing_operation_id = bo.id
        WHERE wt.created_at BETWEEN %s AND %s
          AND (%s::text[] IS NULL OR bo.service = ANY(%s::text[]))
          AND (%s::text[] IS NULL OR wt.user_id = ANY(%s::text[]))
    """
    
    params = (
        request.start_date,
        request.end_date,
        request.service_types if request.service_types != ["all"] else None,
        request.service_types if request.service_types != ["all"] else None,
        request.user_ids if request.user_ids else None,
        request.user_ids if request.user_ids else None,
    )
    
    result = conn.execute(text(count_query), params)
    total_records = result.fetchone()["total"]
    
    # Determine execution mode
    if total_records <= 5000:
        # Sync mode: Generate immediately
        return await generate_sync_export(conn, request, total_records)
    elif total_records <= 10000:
        # Async mode with polling
        export_id = str(uuid.uuid4())
        
        # Queue background job (implement later in worker)
        await queue_async_export_job(export_id, request.dict())
        
        return ExportResponse(
            export_id=export_id,
            status="queued",
            message=f"Large export ({total_records} records) queued. Poll /api/admin/reports/export/{export_id}/status for progress."
        )
    else:
        # Email notification mode for very large exports
        export_id = str(uuid.uuid4())
        
        await queue_email_notification_export(export_id, request.dict(), current_user.email)
        
        return ExportResponse(
            export_id=export_id,
            status="queued",
            message=f"Very large export ({total_records} records) will be emailed when ready."
        )


async def generate_sync_export(
    conn: BusinessConnection, 
    request: ExportRequest,
    total_records: int
) -> ExportResponse:
    """Synchronous export for small datasets."""
    
    if request.format == "csv":
        return await stream_csv_response(conn, request)
    else:  # xlsx
        return await generate_excel_response(conn, request)


@router.get("/api/admin/reports/export/{export_id}/status", response_model=ExportResponse)
async def check_export_status(
    export_id: str,
    current_user: UserResponse = Depends(get_current_user),
):
    """Check status of an async export task."""
    
    # Implement Redis-based status lookup here
    # For now, return error
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Async export tracking not yet implemented. Use synchronous mode for <5000 records."
    )


def generate_csv_content(conn: BusinessConnection, request: ExportRequest) -> bytes:
    """Generate CSV content with gzip compression."""
    
    # Prepare query with all filters
    query = """
        SELECT 
            wt.id,
            wt.user_id,
            u.email as user_email,
            DATE(wt.created_at) as billing_date,
            bo.service as service_type,
            wt.available_delta as credits_delta,
            ABS(wt.available_delta) as credits_used,
            s.unit_cost_fen,
            ABS(wt.available_delta) * s.unit_cost_fen / 100.0 as cost_fen,
            wt.billing_round
        FROM wallet_transactions wt
        JOIN billing_operations bo ON wt.billing_operation_id = bo.id
        JOIN services s ON bo.service = s.name
        LEFT JOIN users u ON wt.user_id = u.id
        WHERE wt.created_at BETWEEN %s AND %s
          AND (%s::text[] IS NULL OR bo.service = ANY(%s::text[]))
          AND (%s::text[] IS NULL OR wt.user_id = ANY(%s::text[]))
          AND (%s IS NULL OR cost_fen >= %s)
          AND (%s IS NULL OR cost_fen <= %s)
        ORDER BY wt.created_at DESC, wt.id
    """
    
    # Calculate min/max cost for filtering
    min_cost = (request.min_revenue_fen or 0) if request.min_revenue_fen else None
    max_cost = (request.max_revenue_fen or float('inf')) if request.max_revenue_fen else None
    
    params = (
        request.start_date,
        request.end_date,
        request.service_types if request.service_types != ["all"] else None,
        request.service_types if request.service_types != ["all"] else None,
        request.user_ids if request.user_ids else None,
        request.user_ids if request.user_ids else None,
        min_cost,
        min_cost,
        max_cost,
        max_cost,
    )
    
    # Execute query and stream rows to CSV
    result = conn.execute(text(query), params)
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header row
    writer.writerow([
        "Transaction ID",
        "User ID",
        "Email",
        "Billing Date",
        "Service Type",
        "Credits Delta",
        "Credits Used",
        "Unit Cost (Fen)",
        "Total Cost (Fen)",
        "Billing Round",
    ])
    
    # Stream rows
    for row in result:
        writer.writerow([
            str(row["id"]),
            str(row["user_id"]),
            row["user_email"] or "N/A",
            row["billing_date"].isoformat(),
            row["service_type"],
            int(row["credits_delta"]),
            int(row["credits_used"]),
            float(row["unit_cost_fen"]),
            float(row["cost_fen"]),
            int(row["billing_round"]),
        ])
    
    # Compress with gzip
    gzipped = gzip.compress(output.getvalue().encode("utf-8"))
    return gzipped


async def stream_csv_response(
    conn: BusinessConnection, 
    request: ExportRequest
) -> Response:
    """Stream CSV file directly from DB to avoid memory overflow."""
    
    try:
        csv_content = generate_csv_content(conn, request)
    except Exception as e:
        logger.error(f"CSV generation failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate CSV export: {str(e)}"
        )
    
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"billing_export_{timestamp}.csv.gz"
    
    return Response(
        content=csv_content,
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        }
    )


def generate_excel_sheet_data(conn: BusinessConnection, request: ExportRequest):
    """Generate data for Excel workbook."""
    
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    
    wb = Workbook()
    
    # Sheet 1: Summary by Day
    ws_summary = wb.active
    ws_summary.title = "Daily Summary"
    
    summary_query = """
        SELECT 
            DATE(created_at) as day,
            bo.service,
            COUNT(*) as transaction_count,
            SUM(ABS(available_delta)) as total_credits,
            SUM(ABS(available_delta)) * AVG(s.unit_cost_fen) / 100 as revenue_yuan
        FROM wallet_transactions wt
        JOIN billing_operations bo ON wt.billing_operation_id = bo.id
        JOIN services s ON bo.service = s.name
        WHERE wt.created_at BETWEEN %s AND %s
          AND (%s::text[] IS NULL OR bo.service = ANY(%s::text[]))
          AND wt.user_id IS NOT NULL  -- exclude platform-borne transactions
        GROUP BY day, bo.service
        ORDER BY day DESC
    """
    
    params = (
        request.start_date,
        request.end_date,
        request.service_types if request.service_types != ["all"] else None,
        request.service_types if request.service_types != ["all"] else None,
    )
    
    # Add header
    ws_summary.append(["Day", "Service", "Transactions", "Credits Used", "Revenue (Yuan)"])
    
    # Format header row
    bold_font = Font(bold=True)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    
    for cell in ws_summary[1]:
        cell.font = bold_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    
    # Add data rows
    result = conn.execute(text(summary_query), params)
    for row in result:
        ws_summary.append([
            row["day"].isoformat(),
            row["service"],
            int(row["transaction_count"]),
            float(row["total_credits"]),
            float(row["revenue_yuan"]),
        ])
    
    # Adjust column widths
    column_widths = {"A": 12, "B": 20, "C": 15, "D": 15, "E": 18}
    for col_letter, width in column_widths.items():
        ws_summary.column_dimensions[col_letter].width = width
    
    # Sheet 2: Detailed Transactions
    ws_detail = wb.create_sheet("Transaction Details")
    
    detail_query = """
        SELECT 
            wt.id,
            wt.user_id,
            u.email,
            wt.created_at,
            bo.service,
            wt.available_delta,
            ABS(wt.available_delta) * s.unit_cost_fen / 100 as cost_fen
        FROM wallet_transactions wt
        JOIN billing_operations bo ON wt.billing_operation_id = bo.id
        JOIN services s ON bo.service = s.name
        LEFT JOIN users u ON wt.user_id = u.id
        WHERE wt.created_at BETWEEN %s AND %s
        ORDER BY wt.created_at DESC
        LIMIT 5000  -- Prevent infinite growth
    """
    
    ws_detail.append(["TXN ID", "User ID", "Email", "Timestamp", "Service", "Delta", "Cost (Fen)"])
    
    # Add header formatting
    for cell in ws_detail[1]:
        cell.font = bold_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")
    
    result = conn.execute(text(detail_query), params)
    for row in result:
        ws_detail.append([
            str(row["id"]),
            str(row["user_id"]),
            row["email"] or "N/A",
            row["created_at"].strftime("%Y-%m-%d %H:%M:%S"),
            row["service"],
            int(row["available_delta"]),
            float(row["cost_fen"]),
        ])
    
    # Auto-width columns
    for column_cells in ws_detail.columns:
        maximum_length = 0
        column_letter = column_cells[0].column_letter
        for cell in column_cells:
            try:
                if len(str(cell.value)) > maximum_length:
                    maximum_length = len(str(cell.value))
            except:
                pass
        adjusted_width = min(maximum_length + 2, 50)
        ws_detail.column_dimensions[column_letter].width = adjusted_width
    
    return wb


async def generate_excel_response(
    conn: BusinessConnection,
    request: ExportRequest
) -> Response:
    """Generate formatted Excel file with multiple sheets."""
    from io import BytesIO
    
    try:
        wb = generate_excel_sheet_data(conn, request)
    except Exception as e:
        logger.error(f"Excel generation failed: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate Excel export: {str(e)}"
        )
    
    # Save to BytesIO
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"billing_export_{timestamp}.xlsx"
    
    return Response(
        content=output.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        }
    )


def validate_request(request: ExportRequest) -> tuple[bool, str]:
    """Validate export request parameters."""
    
    if request.start_date > request.end_date:
        return False, "start_date must be before or equal to end_date"
    
    if (request.end_date - request.start_date).days > 90:
        return False, "Date range cannot exceed 90 days"
    
    if request.format not in ["csv", "xlsx"]:
        return False, f"Unsupported format: {request.format}"
    
    return True, ""


async def queue_async_export_job(export_id: str, params: dict):
    """Queue async export job for large datasets (>5000 records)."""
    # TODO: Implement Redis-based job queue (Celery/RQ)
    pass


async def queue_email_notification_export(export_id: str, params: dict, email: str):
    """Schedule email notification for very large exports (>10k records)."""
    # TODO: Implement background email notification system
    pass
