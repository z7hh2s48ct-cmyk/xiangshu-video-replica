"""Recharge order model - Tenant credit purchase records.

This module defines the RechargeOrder ORM model for tracking all credit
recharge orders made by tenants, including payment status and amounts.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Integer, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RechargeOrder(Base):
    """Record of tenant credit recharge orders.
    
    Attributes:
        id: Primary key UUID (auto-generated)
        order_no: Unique business order number (user-facing)
        tenant_id: Reference to parent tenant (FK)
        amount_fen: Amount purchased in fen
        amount_yuan: Amount in yuan (calculated)
        actual_payment_cents: Actual payment amount in cents
        points_ratio: Exchange rate used (points per yuan)
        bonus_points_fen: Bonus credit granted (if any)
        status: Order status (pending | paid | failed | refunded)
        payment_method: Payment method used (wechat | alipay | bank_transfer)
        payment_reference: External payment reference ID
        note: Internal notes about this order
        expired_at: Expiration timestamp if applicable
        created_at: Order creation timestamp
        updated_at: Last modification timestamp
    
    Relationships:
        tenant: Many-to-one relationship to Tenant
    
    Usage:
        >>> order = RechargeOrder(
        ...     order_no="ORD-20260920-001",
        ...     tenant_id="tenant_example_001",
        ...     amount_fen=50000,  # 500 RMB equivalent
        ... )
        >>> db.add(order)
        >>> db.commit()
    """
    
    __tablename__ = "recharge_orders"
    
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: "uuid4()",
        comment="Primary key UUID",
    )
    
    order_no: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
        comment="Unique business order number",
    )
    
    tenant_id: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Reference to tenant.tenant_id",
    )
    
    amount_fen: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="Credit amount in fen (RMB * 100)",
    )
    
    amount_yuan: Mapped[float] = mapped_column(
        Float(precision=10, scale=2),
        nullable=False,
        comment="Credit amount in yuan/RMB",
    )
    
    actual_payment_cents: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        comment="Actual payment amount in cents",
    )
    
    points_ratio: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=100,
        comment="Exchange rate: points per yuan at time of order",
    )
    
    bonus_points_fen: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
        default=0,
        comment="Bonus credit granted (promotions, etc.)",
    )
    
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        comment="Order status: pending | paid | failed | refunded | cancelled",
    )
    
    payment_method: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
        comment="Payment method: wechat | alipay | bank_transfer",
    )
    
    payment_reference: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="External payment transaction reference ID",
    )
    
    note: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Internal notes about this order",
    )
    
    paid_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when order was marked as paid",
    )
    
    expired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Expiration timestamp if applicable",
    )
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        comment="Order creation timestamp",
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
            f"<RechargeOrder(id={self.id!r}, order_no={self.order_no!r}, "
            f"tenant_id={self.tenant_id!r}, status={self.status!r})>"
        )
    
    @property
    def total_fen(self) -> int:
        """Get total credit including bonus."""
        return self.amount_fen + (self.bonus_points_fen or 0)
    
    @property
    def total_yuan(self) -> float:
        """Get total credit in yuan including bonus."""
        return round(self.total_fen / 100.0, 2)
    
    @property
    def is_paid(self) -> bool:
        """Check if order is paid."""
        return self.status == "paid"
    
    @property
    def is_pending(self) -> bool:
        """Check if order is pending."""
        return self.status == "pending"
    
    @property
    def is_expired(self) -> bool:
        """Check if order has expired."""
        if not self.expired_at:
            return False
        return datetime.utcnow() > self.expired_at
