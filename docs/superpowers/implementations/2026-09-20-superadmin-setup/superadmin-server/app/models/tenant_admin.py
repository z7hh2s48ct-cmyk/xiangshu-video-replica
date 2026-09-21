"""Tenant Admin model - Superadmin user accounts.

This module defines the TenantAdmin ORM model for managing superadmin users
who have full access to the superadmin dashboard and can manage tenants,
pricing configurations, and view reports.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TenantAdmin(Base):
    """Superadmin user account with full system access.
    
    Attributes:
        id: Primary key UUID (auto-generated)
        username: Unique login username (email or custom)
        email: Admin's email address (unique, indexed)
        hashed_password: Bcrypt-hashed password (never stored plain)
        is_active: Whether the account is active and can log in
        is_superuser: Whether this admin has full superuser privileges
        full_name: Display name of the admin
        avatar_url: URL to profile picture or avatar
        notes: Internal notes about this admin (not visible to users)
        last_login_at: Timestamp of last successful login
        created_at: Account creation timestamp
        updated_at: Last modification timestamp
    
    Relationships:
        None - flat structure for simplicity
    
    Usage:
        >>> admin = TenantAdmin(
        ...     username="admin1",
        ...     email="admin@example.com",
        ...     hashed_password="$2b$...",  # bcrypt hash
        ...     is_superuser=True,
        ... )
        >>> db.add(admin)
        >>> db.commit()
    """
    
    __tablename__ = "tenant_admins"
    
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: "uuid4()",  # Will be filled by database function
        comment="Primary key UUID",
    )
    
    username: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        unique=True,
        index=True,
        comment="Unique login username",
    )
    
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
        comment="Email address for notifications and recovery",
    )
    
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Bcrypt-hashed password",
    )
    
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="Whether the account is active",
    )
    
    is_superuser: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Whether this admin has full superuser privileges",
    )
    
    full_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
        comment="Full display name",
    )
    
    avatar_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
        comment="URL to profile picture",
    )
    
    notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
        comment="Internal notes",
    )
    
    last_login_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        default=None,
        onupdate=datetime.utcnow(),
        comment="Last successful login timestamp",
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
    
    @property
    def display_name(self) -> str:
        """Return display name for UI."""
        return self.full_name or self.username or self.email
    
    def __repr__(self) -> str:
        """String representation for debugging."""
        return (
            f"<TenantAdmin(id={self.id!r}, username={self.username!r}, "
            f"email={self.email!r}, is_superuser={self.is_superuser})>"
        )
