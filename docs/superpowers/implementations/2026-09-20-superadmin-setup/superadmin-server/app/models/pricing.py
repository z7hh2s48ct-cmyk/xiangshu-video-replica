"""Pricing configuration model - Tenant pricing settings.

This module defines the PricingConfiguration ORM model for storing price
settings that each tenant uses to bill their own users.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PricingConfiguration(Base):
    """Pricing configuration for a specific tenant.
    
    Attributes:
        id: Primary key UUID (auto-generated)
        tenant_id: Reference to parent tenant (FK)
        name: Configuration name/description
        subject: Price subject (video_768p | video_2k | oral)
        base_price_fen: Base price in fen (e.g., 5000 = 50 RMB)
        currency: Currency code (default CNY)
        exchange_rate: Exchange rate points per yuan (default 100)
        discount_percent: Discount percentage (100 = no discount)
        min_usage_fen: Minimum usage threshold in fen
        max_usage_fen: Maximum usage threshold in fen
        is_active: Whether this pricing is active
        effective_from: Start date of validity
        effective_until: End date of validity
        priority: Priority level for conflict resolution (higher = more priority)
        notes: Internal notes about this pricing
        created_at: Creation timestamp
        updated_at: Last modification timestamp
    
    Relationships:
        tenant: Many-to-one relationship to Tenant
    
    Usage:
        >>> pricing = PricingConfiguration(
        ...     tenant_id="tenant_example_001",
        ...     subject="video_768p",
        ...     base_price_fen=50000,  # 500 RMB
        ... )
        >>> db.add(pricing)
        >>> db.commit()
    """
    
    __tablename__ = "pricing_configurations"
    
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: "uuid4()",
        comment="Primary key UUID",
    )
    
    tenant_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Reference to tenant.tenant_id",
    )
    
    name: Mapped[Optional[str]] = mapped_column(
        String(200),
        nullable=True,
        comment="Configuration name/description",
    )
    
    subject: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Price subject: video_768p | video_2k | oral",
    )
    
    base_price_fen: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Base price in fen (RMB * 100)",
    )
    
    currency: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="CNY",
        comment="Currency code (ISO 4217)",
    )
    
    exchange_rate: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        comment="Exchange rate: points per yuan (1 yuan = X points)",
    )
    
    discount_percent: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        comment="Discount percentage (1-100, 100 = full price)",
    )
    
    min_usage_fen: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Minimum usage threshold in fen",
    )
    
    max_usage_fen: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        comment="Maximum usage threshold in fen",
    )
    
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether this pricing is currently active",
    )
    
    effective_from: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Start date of validity",
    )
    
    effective_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="End date of validity",
    )
    
    priority: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        comment="Priority level (higher = more priority in conflicts)",
    )
    
    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Internal notes",
    )
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Creation timestamp",
    )
    
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow(),
        comment="Last update timestamp",
    )
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"<PricingConfiguration(id={self.id!r}, tenant_id={self.tenant_id!r}, "
            f"subject={self.subject!r}, price_fen={self.base_price_fen})>"
        )
    
    @property
    def effective_price_fen(self) -> int:
        """Calculate actual price after discount."""
        return int(self.base_price_fen * self.discount_percent / 100)
    
    @property
    def effective_price_yuan(self) -> float:
        """Calculate actual price in yuan/RMB after discount."""
        return round(self.effective_price_fen / 100.0, 2)
