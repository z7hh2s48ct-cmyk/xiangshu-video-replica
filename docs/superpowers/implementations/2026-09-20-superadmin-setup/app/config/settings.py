"""Configuration management using Pydantic Settings.

This module handles environment variable loading, validation, and encryption
of sensitive credentials like API keys and JWT secrets.

Key features:
- Type-safe configuration via Pydantic models
- Environment variable precedence (explicit > default)
- Automatic decryption of encrypted API keys
- Runtime validation of required fields

Author: AI Implementation Team
Date: 2026-09-20
"""

import os
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings
from cryptography.fernet import Fernet


class Settings(BaseSettings):
    """Application settings loaded from environment variables.
    
    All sensitive data (secrets, tokens, passwords) should be stored in
    .env.local or OS environment variables and never committed to git.
    
    Access pattern:
        >>> from app.config.settings import settings
        >>> print(settings.database_url)
    """
    
    # =====================================================
    # Application Configuration
    # =====================================================
    
    app_name: str = Field(
        default="Superadmin API",
        description="Application name displayed in logs and Swagger docs"
    )
    
    debug: bool = Field(
        default=False,
        description="Enable debug mode (verbose logging, auto-reload)"
    )
    
    api_host: str = Field(
        default="0.0.0.0",
        description="Host address for API server binding"
    )
    
    api_port: int = Field(
        default=8001,
        ge=1,
        le=65535,
        description="Port number for API server"
    )
    
    # =====================================================
    # Database Configuration
    # =====================================================
    
    database_url: str = Field(
        default="postgresql://superadmin:password@localhost:5432/superadmin_dev",
        description="PostgreSQL connection string"
    )
    
    pool_size: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Number of connections in async connection pool"
    )
    
    max_overflow: int = Field(
        default=20,
        ge=0,
        description="Maximum overflow connections beyond pool_size"
    )
    
    # =====================================================
    # Authentication & Security
    # =====================================================
    
    jwt_secret_key: str = Field(
        default="dev-secret-key-change-in-production-minimum-32-chars-long",
        min_length=32,
        description="Secret key for JWT token signing (minimum 32 chars recommended)"
    )
    
    jwt_algorithm: str = Field(
        default="HS256",
        description="JWT signing algorithm (HS256 or RS256)"
    )
    
    jwt_exp_minutes: int = Field(
        default=60,
        ge=1,
        description="Access token expiration time in minutes"
    )
    
    refresh_token_exp_days: int = Field(
        default=7,
        ge=1,
        description="Refresh token expiration time in days"
    )
    
    # =====================================================
    # Superadmin API Integration
    # =====================================================
    
    superadmin_api_url: str = Field(
        default="https://superadmin.yourdomain.com",
        description="Base URL of the Superadmin system (hosted separately)"
    )
    
    fernet_master_key: str | None = Field(
        default=None,
        description="Master encryption key for Fernet symmetric encryption"
    )
    
    # =====================================================
    # Rate Limiting Configuration
    # =====================================================
    
    rate_limit_default: str = Field(
        default="100 per minute",
        description="Default rate limit for most endpoints"
    )
    
    rate_limit_auth: str = Field(
        default="10 per minute",
        description="Stricter limit for authentication endpoints"
    )
    
    rate_limit_export: str = Field(
        default="5 per hour",
        description="Very restrictive limit for heavy export operations"
    )
    
    # =====================================================
    # Feature Flags
    # =====================================================
    
    enable_audit_logging: bool = Field(
        default=True,
        description="Enable audit log middleware for all write operations"
    )
    
    enable_prometheus_metrics: bool = Field(
        default=True,
        description="Expose Prometheus metrics at /metrics endpoint"
    )
    
    cors_allow_origins: list[str] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:5173",
            "https://admin.yourdomain.com",
        ],
        description="List of allowed CORS origins (comma-separated in env)"
    )
    
    # =====================================================
    # Field Validators (runtime validation)
    # =====================================================
    
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Validate that database URL uses PostgreSQL protocol."""
        if not v.startswith("postgresql://"):
            raise ValueError("database_url must use PostgreSQL protocol")
        return v
    
    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        """Enforce minimum length for security."""
        if len(v) < 32:
            raise ValueError("JWT secret key must be at least 32 characters long")
        return v
    
    @field_validator("cors_allow_origins")
    @classmethod
    def parse_cors_origins(cls, v: str | list[str]) -> list[str]:
        """Parse comma-separated string into list of origins."""
        if isinstance(v, str):
            # Handle single string value from environment
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v
    
    # =====================================================
    # Utility Methods
    # =====================================================
    
    def decrypt_api_key(self, encrypted_key: str) -> str:
        """Decrypt an API key using Fernet symmetric encryption.
        
        Args:
            encrypted_key: Base64-encoded Fernet encrypted string
            
        Returns:
            Decrypted plaintext API key
            
        Raises:
            RuntimeError: If FERNET_MASTER_KEY is not configured
        """
        if not self.fernet_master_key:
            raise RuntimeError("FERNET_MASTER_KEY not configured in environment")
        
        fernet = Fernet(self.fernet_master_key.encode())
        return fernet.decrypt(encrypted_key.encode()).decode()
    
    def get_full_url(self, path: str) -> str:
        """Construct full URL including scheme and port.
        
        Args:
            path: API path relative to root (e.g., '/api/v1/tenants')
            
        Returns:
            Full URL with http:// prefix
        """
        protocol = "https" if self.debug else "http"
        host_port = f"{self.api_host}:{self.api_port}"
        return f"{protocol}://{host_port}{path}"


# Singleton instance
settings = Settings()

__all__ = ["settings", "Settings"]
