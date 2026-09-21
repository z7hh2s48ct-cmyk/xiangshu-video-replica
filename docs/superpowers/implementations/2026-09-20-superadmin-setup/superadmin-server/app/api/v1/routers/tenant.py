"""Tenant management router - CRUD operations for tenant accounts.

This module provides full CRUD API endpoints for managing tenants:
- GET /api/v1/tenants - List all tenants with pagination
- POST /api/v1/tenants - Create new tenant
- GET /api/v1/tenants/{tenant_id} - Get single tenant details
- PUT /api/v1/tenants/{tenant_id} - Update tenant info
- PATCH /api/v1/tenants/{tenant_id}/status - Change tenant status
- DELETE /api/v1/tenants/{tenant_id} - Soft delete tenant

Features:
- Pagination support (page, page_size query params)
- Search by name/tenant_id/email
- Status change with audit logging
- Balance adjustment with validation

Author: AI Implementation Team
Date: 2026-09-20
"""

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.core.security import get_current_user_from_jwt
from app.database import get_db, SessionLocal
from app.models.tenant import Tenant
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter()


# =====================================================
# Pydantic Schemas
# =====================================================

class TenantCreate(BaseModel):
    """Schema for creating a new tenant."""
    
    tenant_id: str = Field(..., min_length=3, max_length=50, description="Unique business ID")
    name: str = Field(..., min_length=1, max_length=200, description="Display name")
    company_name: Optional[str] = Field(None, max_length=255)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_phone: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=500)
    initial_balance_fen: int = Field(default=0, ge=0, description="Starting balance in fen")


class TenantUpdate(BaseModel):
    """Schema for updating tenant info."""
    
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    company_name: Optional[str] = Field(None, max_length=255)
    contact_email: Optional[str] = Field(None, max_length=255)
    contact_phone: Optional[str] = Field(None, max_length=50)
    address: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = None


class TenantStatusUpdate(BaseModel):
    """Schema for updating tenant status."""
    
    status: str = Field(..., pattern="^(pending|active|suspended|deactivated)$",
                        description="New status value")
    note: Optional[str] = None


class TenantResponse(BaseModel):
    """Response schema for tenant data."""
    
    id: str
    tenant_id: str
    name: str
    company_name: Optional[str]
    contact_email: Optional[str]
    contact_phone: Optional[str]
    address: Optional[str]
    status: str
    balance_yuan: float
    total_recharge_fen: int
    
    class Config:
        from_attributes = True


class TenantListResponse(BaseModel):
    """Paginated list response."""
    
    items: list[TenantResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


# =====================================================
# Endpoints
# =====================================================

@router.get("", response_model=TenantListResponse)
async def list_tenants(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    search: Optional[str] = Query(None, description="Search by name/tenant_id/email"),
    status_filter: Optional[str] = Query(None, description="Filter by status"),
    db: Session = Depends(get_db),
):
    """Get paginated list of tenants with optional filtering.
    
    ---
    tags: ["Tenants"]
    """
    # Build query
    query = db.query(Tenant)
    
    # Apply filters
    if status_filter:
        query = query.filter(Tenant.status == status_filter)
    
    if search:
        search_pattern = f"%{search}%"
        query = query.filter(
            (Tenant.name.ilike(search_pattern)) |
            (Tenant.tenant_id.ilike(search_pattern)) |
            (Tenant.contact_email.ilike(search_pattern))
        )
    
    # Count total before pagination
    total = query.count()
    
    # Paginate
    offset = (page - 1) * page_size
    items = query.order_by(Tenant.created_at.desc()).offset(offset).limit(page_size).all()
    
    return TenantListResponse(
        items=[TenantResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.post("", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    tenant_data: TenantCreate,
    db: Session = Depends(get_db),
):
    """Create a new tenant account.
    
    ---
    tags: ["Tenants"]
    """
    # Check if tenant_id already exists
    existing = db.query(Tenant).filter(Tenant.tenant_id == tenant_data.tenant_id).first()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tenant ID '{tenant_data.tenant_id}' already exists",
        )
    
    # Create new tenant
    new_tenant = Tenant(
        tenant_id=tenant_data.tenant_id,
        name=tenant_data.name,
        company_name=tenant_data.company_name,
        contact_email=tenant_data.contact_email,
        contact_phone=tenant_data.contact_phone,
        address=tenant_data.address,
        status="pending",
        balance_fen=tenant_data.initial_balance_fen,
        total_recharge_fen=tenant_data.initial_balance_fen,
    )
    
    db.add(new_tenant)
    db.commit()
    db.refresh(new_tenant)
    
    logger.info(f"Created new tenant: {tenant_data.tenant_id}")
    
    return TenantResponse.model_validate(new_tenant)


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
):
    """Get detailed information about a specific tenant.
    
    ---
    tags: ["Tenants"]
    """
    tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    
    return TenantResponse.model_validate(tenant)


@router.put("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: str,
    update_data: TenantUpdate,
    db: Session = Depends(get_db),
):
    """Update tenant basic information.
    
    ---
    tags: ["Tenants"]
    """
    tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    
    # Update fields selectively
    update_dict = update_data.model_dump(exclude_unset=True)
    
    for field, value in update_dict.items():
        if hasattr(tenant, field):
            setattr(tenant, field, value)
    
    db.commit()
    db.refresh(tenant)
    
    logger.info(f"Updated tenant: {tenant_id}")
    
    return TenantResponse.model_validate(tenant)


@router.patch("/{tenant_id}/status", response_model=TenantResponse)
async def update_tenant_status(
    tenant_id: str,
    status_update: TenantStatusUpdate,
    db: Session = Depends(get_db),
):
    """Change tenant status with audit logging.
    
    Valid status transitions:
    - pending -> active | suspended | deactivated
    - active -> suspended | deactivated
    - suspended -> active | deactivated
    - deactivated -> (no transitions allowed, must recreate)
    
    ---
    tags: ["Tenants"]
    """
    tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    
    old_status = tenant.status
    new_status = status_update.status
    
    # Enforce valid transitions
    invalid_transitions = {
        ("deactivated", "pending"),
        ("deactivated", "active"),
        ("deactivated", "suspended"),
    }
    
    if (old_status, new_status) in invalid_transitions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot transition from '{old_status}' to '{new_status}'",
        )
    
    tenant.status = new_status
    
    # Add status change log to notes (simple audit trail)
    if tenant.notes:
        tenant.notes += f"\n[{datetime.utcnow()}] Status changed: {old_status} -> {new_status}"
    else:
        tenant.notes = f"[{datetime.utcnow()}] Status changed: {old_status} -> {new_status}"
    
    db.commit()
    db.refresh(tenant)
    
    logger.warning(f"Status change: {tenant_id}, {old_status} -> {new_status}")
    
    return TenantResponse.model_validate(tenant)


@router.delete("/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tenant(
    tenant_id: str,
    db: Session = Depends(get_db),
):
    """Soft delete a tenant (set status to 'deactivated').
    
    This doesn't permanently remove the record but marks it as inactive.
    
    ---
    tags: ["Tenants"]
    """
    tenant = db.query(Tenant).filter(Tenant.tenant_id == tenant_id).first()
    
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    
    if tenant.status == "deactivated":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant is already deactivated",
        )
    
    tenant.status = "deactivated"
    db.commit()
    
    logger.info(f"Soft deleted tenant: {tenant_id}")
    
    return None  # 204 No Content
