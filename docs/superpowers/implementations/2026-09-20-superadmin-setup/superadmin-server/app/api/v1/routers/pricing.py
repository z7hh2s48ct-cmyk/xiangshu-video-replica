"""Pricing configuration router - Manage tenant pricing settings.

Endpoints:
- GET /api/v1/pricing - List all pricing configurations
- POST /api/v1/pricing - Create new pricing
- PUT /api/v1/pricing/{pricing_id} - Update pricing
- DELETE /api/v1/pricing/{pricing_id} - Delete pricing

Supports multiple price subjects: video_768p, video_2k, oral

Author: AI Implementation Team
Date: 2026-09-20
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.database import get_db, SessionLocal
from app.models.pricing import PricingConfiguration
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

router = APIRouter()


class PricingCreate(BaseModel):
    """Schema for creating pricing configuration."""
    
    tenant_id: str = Field(..., description="Target tenant ID")
    subject: str = Field(..., pattern="^(video_768p|video_2k|oral)$")
    base_price_fen: int = Field(..., ge=0, description="Base price in fen")
    discount_percent: int = Field(default=100, ge=1, le=100)
    currency: str = Field(default="CNY", max_length=10)
    exchange_rate: int = Field(default=100, ge=1)
    notes: Optional[str] = None
    
    @field_validator('subject')
    @classmethod
    def validate_subject(cls, v):
        valid_subjects = ['video_768p', 'video_2k', 'oral']
        if v not in valid_subjects:
            raise ValueError(f"Subject must be one of: {valid_subjects}")
        return v


class PricingUpdate(BaseModel):
    """Schema for updating pricing."""
    
    base_price_fen: Optional[int] = Field(None, ge=0)
    discount_percent: Optional[int] = Field(None, ge=1, le=100)
    exchange_rate: Optional[int] = Field(None, ge=1)
    is_active: Optional[bool] = None
    notes: Optional[str] = None


class PricingResponse(BaseModel):
    """Pricing response with calculated fields."""
    
    id: str
    tenant_id: str
    name: Optional[str]
    subject: str
    base_price_fen: int
    effective_price_fen: int
    effective_price_yuan: float
    discount_percent: int
    is_active: bool
    
    class Config:
        from_attributes = True


@router.get("", response_model=list[PricingResponse])
async def list_pricing(
    tenant_id: Optional[str] = None,
    subject: Optional[str] = None,
    is_active_only: bool = False,
    db: Session = Depends(get_db),
):
    """List pricing configurations with optional filters."""
    query = db.query(PricingConfiguration)
    
    if tenant_id:
        query = query.filter(PricingConfiguration.tenant_id == tenant_id)
    
    if subject:
        query = query.filter(PricingConfiguration.subject == subject)
    
    if is_active_only:
        query = query.filter(PricingConfiguration.is_active == True)
    
    configs = query.order_by(PricingConfiguration.priority.desc()).all()
    
    return [PricingResponse.model_validate(c) for c in configs]


@router.post("", response_model=PricingResponse, status_code=status.HTTP_201_CREATED)
async def create_pricing(
    pricing_data: PricingCreate,
    db: Session = Depends(get_db),
):
    """Create a new pricing configuration."""
    # Check for existing config with same tenant_id + subject
    existing = db.query(PricingConfiguration).filter(
        PricingConfiguration.tenant_id == pricing_data.tenant_id,
        PricingConfiguration.subject == pricing_data.subject,
    ).first()
    
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Price configuration already exists for tenant {pricing_data.tenant_id} and subject {pricing_data.subject}",
        )
    
    new_config = PricingConfiguration(
        tenant_id=pricing_data.tenant_id,
        subject=pricing_data.subject,
        base_price_fen=pricing_data.base_price_fen,
        discount_percent=pricing_data.discount_percent,
        currency=pricing_data.currency,
        exchange_rate=pricing_data.exchange_rate,
        notes=pricing_data.notes,
        is_active=True,
        priority=0,
    )
    
    db.add(new_config)
    db.commit()
    db.refresh(new_config)
    
    logger.info(f"Created pricing for tenant {pricing_data.tenant_id}, subject {pricing_data.subject}")
    
    return PricingResponse.model_validate(new_config)


@router.put("/{pricing_id}", response_model=PricingResponse)
async def update_pricing(
    pricing_id: str,
    update_data: PricingUpdate,
    db: Session = Depends(get_db),
):
    """Update pricing configuration."""
    config = db.query(PricingConfiguration).filter(PricingConfiguration.id == pricing_id).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pricing configuration '{pricing_id}' not found",
        )
    
    update_dict = update_data.model_dump(exclude_unset=True)
    
    for field, value in update_dict.items():
        if hasattr(config, field):
            setattr(config, field, value)
    
    db.commit()
    db.refresh(config)
    
    return PricingResponse.model_validate(config)


@router.delete("/{pricing_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_pricing(
    pricing_id: str,
    db: Session = Depends(get_db),
):
    """Delete pricing configuration (soft delete by setting is_active=False)."""
    config = db.query(PricingConfiguration).filter(PricingConfiguration.id == pricing_id).first()
    
    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pricing configuration '{pricing_id}' not found",
        )
    
    config.is_active = False
    db.commit()
    
    logger.info(f"Soft deleted pricing: {pricing_id}")
    
    return None
