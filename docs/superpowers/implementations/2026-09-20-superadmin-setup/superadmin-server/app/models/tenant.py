"""Tenant model - Franchisee/customer accounts.

This module defines the Tenant ORM model for representing franchisee or
customer accounts that can configure pricing for their tenants and track
recharge orders.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Tenant(Base):
    """Tenant/franchisee account in the superadmin system.
    
    Attributes:
        id: Primary key UUID (auto-generated)
        tenant_id: Unique business identifier (e.g., "tenant_platform")
        name: Display name of the tenant
        company_name: Legal company name if applicable
        contact_email: Primary contact email
        contact_phone: Contact phone number
        address: Business address
        status: Account status (active, suspended, pending)
        balance_fen: Current credit balance in fen (1 RMB = 100 fen)
        total_recharge_fen: Lifetime recharge amount
        settings_json: JSONB for extensible configuration
        notes: Internal notes about this tenant
        created_at: Account creation timestamp
        updated_at: Last modification timestamp
    
    Relationships:
        pricing_configurations: One-to-many relationship to pricing configs
        recharge_orders: One-to-many relationship to order history
    
    Usage:
        >>> tenant = Tenant(
        ...     tenant_id="tenant_example_001",
        ...     name="Example Franchisee",
        ...     company_name="Example Co Ltd",
        ...     contact_email="contact@example.com",
        ... )
        >>> db.add(tenant)
        >>> db.commit()
    """
    
    __tablename__ = "tenants"
    
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: "uuid4()",
        comment="Primary key UUID",
    )
    
    tenant_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
        comment="Unique business identifier",
    )
    
    name: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        comment="Display name",
    )
    
    company_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        comment="Legal company name",
    )
    
    contact_email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="Contact email address",
    )
    
    contact_phone: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Contact phone number",
    )
    
    address: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment="Business address",
    )
    
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        comment="Account status: pending | active | suspended | deactivated",
    )
    
    balance_fen: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Current credit balance in fen (RMB * 100)",
    )
    
    total_recharge_fen: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Lifetime recharge amount in fen",
    )
    
    settings_json: Mapped[Optional[str]] = mapped_column(
        "settings",  # SQL column name
        Text,
        nullable=True,
        comment="JSON configuration (price subjects, limits, etc.)",
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
        comment="Account creation timestamp",
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
        return f"<Tenant(id={self.id!r}, tenant_id={self.tenant_id!r}, name={self.name!r})>"
    
    @property
    def is_active(self) -> bool:
        """Check if tenant is active."""
        return self.status == "active"
    
    @property
    def balance_yuan(self) -> float:
        """Convert balance from fen to yuan/RMB."""
        return round(self.balance_fen / 100.0, 2)
