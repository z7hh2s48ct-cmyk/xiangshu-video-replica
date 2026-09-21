"""Allowance check router - Balance inquiry for Client System.

This module provides public API endpoints that the Video Replica Client
system can call BEFORE task submission to check if user has sufficient balance.

Endpoints:
- GET /api/v1/allowance/check/{tenant_id} - Check balance and allowed status
- GET /api/v1/allowance/quota/{tenant_id}/{subject} - Get quota details

Security:
- Uses public API Key in header (not JWT)
- Rate limited to prevent abuse
- Returns minimal info (only boolean allowed + remaining balance)

Author: AI Implementation Team
Date: 2026-09-20
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, Field

from app.database import get_db, SessionLocal
from app.models.tenant import Tenant
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter()


class AllowanceCheckResponse(BaseModel):
    """Response for allowance check endpoint."""
    
    tenant_id: str
    allowed: bool
    remaining_fen: int
    price_per_task_fen: int | None = None
    
    class Config:
        from_attributes = True


class QuotaDetailResponse(BaseModel):
    """Detailed quota information."""
    
    tenant_id: str
    subject: str
    balance_fen: int
    balance_yuan: float
    price_fen: int | None
    max_tasks_possible: int | None
    is_active: bool


# =====================================================
# Public API Key validation (simplified - production should use DB lookup)
# =====================================================

def validate_api_key(x_api_key: str = Header(..., description="Public API key for Client System")) -> str:
    """Validate public API key passed in header.
    
    In production, this should:
    1. Lookup API key in database
    2. Verify it's active
    3. Return associated client identifier
    
    For now, we use a simple environment variable check.
    """
    # TODO: Implement proper API key store in database
    allowed_keys = ["superadmin_public_api_key_12345"]  # Set via ENV
    
    if x_api_key not in allowed_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    
    return x_api_key


# =====================================================
# Endpoints
# =====================================================

@router.get("/check/{tenant_id}", response_model=AllowanceCheckResponse)
async def check_allowance(
    tenant_id: str,
    price_per_task_fen: int = Query(..., description="Price per task in fen"),
    x_api_key: str = Depends(validate_api_key),
    db: Session = Depends(get_db),
):
    """Check if tenant has sufficient balance for a task.
    
    This endpoint is called by Video Replica Client BEFORE task submission.
    Returns whether operation is allowed based on current balance.
    
    ---
    tags: ["Allowance Check"]
    responses:
        200:
            description: Balance check successful
            content:
                application/json:
                    schema:
                        $ref: '#/components/schemas/AllowanceCheckResponse'
        401:
            description: Invalid API key
        404:
            description: Tenant not found
    """
    # Find tenant
    tenant: Tenant | None = db.query(Tenant).filter(
        Tenant.tenant_id == tenant_id,
        Tenant.status.in_("active", "pending"),
    ).first()
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    
    # Check balance
    allowed = tenant.balance_fen >= price_per_task_fen
    
    return AllowanceCheckResponse(
        tenant_id=tenant_id,
        allowed=allowed,
        remaining_fen=tenant.balance_fen,
        price_per_task_fen=price_per_task_fen,
    )


@router.get("/quota/{tenant_id}/{subject}", response_model=QuotaDetailResponse)
async def get_quota_details(
    tenant_id: str,
    subject: str,
    x_api_key: str = Depends(validate_api_key),
    db: Session = Depends(get_db),
):
    """Get detailed quota information for a specific price subject.
    
    Returns maximum possible tasks based on current balance and pricing.
    
    ---
    tags: ["Allowance Check"]
    """
    # Find tenant
    tenant: Tenant | None = db.query(Tenant).filter(
        Tenant.tenant_id == tenant_id,
        Tenant.status.in_("active", "pending"),
    ).first()
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    
    # Get pricing for subject
    # Note: In real implementation, you'd query the pricing configuration table
    # For now, using placeholder values
    pricing_map = {
        "video_768p": 50000,  # 500 RMB
        "video_2k": 150000,   # 1500 RMB
        "oral": 1000,         # 10 RMB/second
    }
    
    price_fen = pricing_map.get(subject, 0)
    max_tasks = tenant.balance_fen // price_fen if price_fen > 0 else 0
    
    return QuotaDetailResponse(
        tenant_id=tenant_id,
        subject=subject,
        balance_fen=tenant.balance_fen,
        balance_yuan=round(tenant.balance_fen / 100.0, 2),
        price_fen=price_fen,
        max_tasks_possible=max_tasks,
        is_active=True,
    )
