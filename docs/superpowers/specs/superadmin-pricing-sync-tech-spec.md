# 超管定价同步模块技术设计方案

**日期**: 2026-09-20  
**版本**: V1.0  
**状态**: Draft for Review  
**分支**: `feat/superadmin-pricing-sync`  
**Worktree**: `.worktrees/SUPERADMIN-PRICING-SYNC-20260920`

---

## 📋 目录

1. [总体架构](#1-总体架构)
2. [数据库设计](#2-数据库设计)
3. [核心模块设计](#3-核心模块设计)
4. [API 契约](#4-api-契约)
5. [安全机制](#5-安全机制)
6. [异常处理与降级策略](#6-异常处理与降级策略)
7. [测试方案](#7-测试方案)
8. [部署与运维](#8-部署与运维)

---

## 1. 总体架构

### 1.1 系统设计目标

| 目标 | 描述 | 验收标准 |
|-----|------|---------|
| **低侵入性** | 对现有计费逻辑改动最小化 | 仅修改 3 个文件，新增 1 个模块 |
| **高可用性** | 超管系统不可用时自动降级 | 降级成功率 > 99.9% |
| **实时性** | 价格变更快速生效 | 定时刷新间隔 ≤ 5 分钟 |
| **安全性** | 防止未授权访问和数据泄露 | API Key 加密存储 + HTTPS |
| **可追溯** | 完整的同步日志和审计追踪 | 每次同步记录时间/结果/差异 |

### 1.2 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    Superadmin System                         │
│   ┌──────────────────────────────────────────────────────┐  │
│   │  Admin Portal (新建)                                  │  │
│   │  - 创建/编辑加盟商                                     │  │
│   │  - 配置零售价（video_768p / video_2k / oral）         │  │
│   │  - 充值收款管理                                        │  │
│   └──────────────┬───────────────────────────────────────┘  │
│                  │                                            │
│            REST API Endpoint                                 │
│      GET /api/super/pricing/:tenant_id                       │
│         Authentication: Bearer Token                         │
│                  │                                            │
└──────────────────┼───────────────────────────────────────────┘
                   │ HTTP Request (每 5 分钟)
                   ↓
┌─────────────────────────────────────────────────────────────┐
│              Client Server (Existing)                        │
│   ┌──────────────────────────────────────────────────────┐  │
│   │  TenantPricingSyncService (NEW)                       │  │
│   │  - tenant_pricing.py (核心同步逻辑)                     │  │
│   │  - Cron Job Scheduler (定时任务)                       │  │
│   └──────────────┬───────────────────────────────────────┘  │
│                  │                                            │
│        Local DB Cache                                       │
│    ┌─────────────────────────────────────┐                  │
│    │ tenant_pricing table                │                  │
│    │ - tenant_id (PK)                    │                  │
│    │ - version                           │                  │
│    │ - config_json (JSONB)               │                  │
│    │ - updated_at                        │                  │
│    └─────────────────┬───────────────────┘                  │
│                      │                                      │
│                      ↓                                      │
│   ┌──────────────────────────────────────────────────────┐  │
│   │  Existing Billing Logic (UNCHANGED)                  │  │
│   │  - customer_pricing.py (read_pricing with tenant_id) │  │
│   │  - generation.py (reserve_internal_billing)           │  │
│   │  - oral.py (reserve_oral_billing)                     │  │
│   └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### 1.3 数据流图

```
Step 1: Superadmin Configures Pricing
  Admin UI → POST /api/super/pricing → Update superadmin_db
  
Step 2: Sync Triggered (Cron Job or Manual)
  Cron Job → GET /api/super/pricing/:tenant_id ← Verify API Key
  
Step 3: Validate & Transform
  Response Validation → Pydantic Model → Convert Units
  
Step 4: Cache to Local DB
  INSERT/UPDATE tenant_pricing → Set updated_at = NOW()
  
Step 5: Serve Billing Requests
  generate_task() → read_pricing(tenant_id="tenant_aaa") 
                  → SELECT FROM tenant_pricing WHERE tenant_id="tenant_aaa"
                  → Return PricingConfig
```

---

## 2. 数据库设计

### 2.1 新表结构

```sql
-- File: server/alembic/versions/0XX_tenant_pricing_cache.py

"""Tenant pricing cache from superadmin system

Revision ID: 0xx_tenant_pricing_cache
Revises: 059_operation_cost_records
Create Date: 2026-09-20

"""
from alembic import op
import sqlalchemy as sa

revision = '0xx_tenant_pricing_cache'
down_revision = '059_operation_cost_records'
branch_labels = None
depends_on = None


def upgrade():
    # Create tenant_pricing cache table
    op.create_table(
        'tenant_pricing',
        sa.Column('tenant_id', sa.TEXT(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('config_json', sa.JSONB(), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, 
                 server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('tenant_id'),
        sa.UniqueConstraint('tenant_id', 'version')
    )
    
    # Create index for efficient lookup
    op.create_index(
        'idx_tenant_pricing_tenant_id',
        'tenant_pricing',
        ['tenant_id']
    )
    
    # Insert default pricing for platform tenant
    # This preserves existing behavior for legacy data
    op.execute("""
        INSERT INTO tenant_pricing (tenant_id, version, config_json, updated_at)
        SELECT 'tenant_platform', 
               (SELECT version FROM customer_credit_pricing WHERE id = 1),
               (SELECT config_json FROM customer_credit_pricing WHERE id = 1),
               CURRENT_TIMESTAMP
        WHERE NOT EXISTS (
            SELECT 1 FROM tenant_pricing WHERE tenant_id = 'tenant_platform'
        )
    """)


def downgrade():
    op.drop_index('idx_tenant_pricing_tenant_id')
    op.drop_table('tenant_pricing')
```

### 2.2 Schema 约束说明

| 字段 | 类型 | 约束 | 说明 |
|-----|------|------|------|
| `tenant_id` | TEXT | PRIMARY KEY, NOT NULL | 租户唯一标识 |
| `version` | INTEGER | DEFAULT 1, NOT NULL | 乐观锁版本号，每次更新自增 |
| `config_json` | JSONB | NOT NULL | 定价配置 JSON，结构见 3.1 节 |
| `updated_at` | TIMESTAMPTZ | DEFAULT NOW() | 最后更新时间，用于失效判断 |

### 2.3 索引策略

- **主键索引**: `tenant_id` (自动生成)
- **查询优化索引**: `tenant_pricing(tenant_id)` 已存在
- **未来扩展**: 如按更新时间范围查询，可加复合索引 `(updated_at DESC, tenant_id)`

---

## 3. 核心模块设计

### 3.1 数据模型定义

#### `server/app/tenant_pricing_types.py`

```python
"""Type definitions for tenant pricing sync module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Subject = Literal["video_768p", "video_2k", "oral"]


class PricingConfig(BaseModel):
    """Pricing configuration for a single service subject."""
    
    model_config = {
        "extra": "forbid",
        "strict": True,
        "json_schema_mode": "validation"
    }
    
    video_768p: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,  # Maximum 10,000 RMB per task
        description="Price in fen (1 RMB = 100 fen) for 768p video generation"
    )
    video_2k: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="Price in fen for 2K video generation"
    )
    oral: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="Price in fen per second for oral avatar generation"
    )
    points_per_yuan: int = Field(
        default=100,
        ge=1,
        le=1_000_000,
        description="Points per 1 RMB (default: 100 points/RMB)"
    )
    discount_basis_points: int = Field(
        default=10_000,
        ge=1,
        le=10_000,
        description="Discount basis points (10000 = 100%, no discount)"
    )
    consumption_rounding: Literal["ceil", "floor"] = Field(
        default="ceil",
        description="Rounding mode for fractional credits"
    )
    
    @field_validator('points_per_yuan')
    @classmethod
    def validate_points_ratio(cls, v: int) -> int:
        if v < 1:
            raise ValueError("points_per_yuan must be at least 1")
        return v
    
    @field_validator('discount_basis_points')
    @classmethod
    def validate_discount(cls, v: int) -> int:
        if not (1 <= v <= 10_000):
            raise ValueError("discount_basis_points must be between 1 and 10000")
        return v


class TenantPricingSnapshot(BaseModel):
    """Complete pricing snapshot for a specific tenant."""
    
    model_config = {
        "extra": "forbid",
        "strict": True
    }
    
    tenant_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r'^[a-zA-Z0-9_-]+$',
        description="Tenant identifier (e.g., 'tenant_platform', 'tenant_aaa')"
    )
    name: str = Field(..., description="Tenant display name")
    status: Literal["active", "suspended", "closed"] = Field(
        default="active",
        description="Tenant status (inactive tenants can't submit tasks)"
    )
    pricing: PricingConfig = Field(..., description="Active pricing configuration")
    effective_from: datetime = Field(
        default_factory=datetime.utcnow,
        description="Price effective start time"
    )
    expires_at: datetime | None = Field(
        default=None,
        description="Price expiration time (optional)"
    )
    updated_by: str | None = Field(
        default=None,
        description="Superadmin user who configured this pricing"
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Last modification timestamp"
    )
    
    @property
    def is_expired(self) -> bool:
        """Check if the current pricing has expired."""
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at
    
    @property
    def is_valid_for_submit(self) -> bool:
        """Check if tenant can accept new task submissions."""
        return self.status == "active" and not self.is_expired


@dataclass(frozen=True)
class SyncResult:
    """Result of a pricing synchronization operation."""
    
    tenant_id: str
    success: bool
    prices_fetched: bool
    db_updated: bool
    version_before: int
    version_after: int
    error_message: str | None = None
    
    @property
    def full_success(self) -> bool:
        return self.success and self.prices_fetched and self.db_updated
    
    @property
    def degraded_success(self) -> bool:
        """Partial success: fetched but failed to write (acceptable)."""
        return self.success and self.prices_fetched


class AllowanceCheckResponse(BaseModel):
    """Response from allowance check API."""
    
    allowed: bool
    remaining_fen: int
    credit_limit_fen: int | None = None
    message: str | None = None
```

### 3.2 Superadmin Client Module

#### `server/app/superadmin_client.py`

```python
"""HTTP client for communicating with Superadmin system.

This module handles all outbound requests to the Superadmin API,
including pricing retrieval and allowance checks.

Security considerations:
- All API keys are stored in encrypted environment variables
- Connections use TLS 1.2+ only
- Request timeouts enforced to prevent hanging
- Retry logic with exponential backoff
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
from pydantic import ValidationError

from app.tenant_pricing_types import (
    AllowanceCheckResponse,
    TenantPricingSnapshot,
    SyncResult,
)

logger = logging.getLogger(__name__)

# Timeout configuration
TIMEOUT_SECONDS = 10.0
MAX_RETRIES = 3
BASE_BACKOFF_MS = 100


@dataclass
class SuperAdminClientConfig:
    """Configuration for Superadmin API client."""
    
    base_url: str
    api_key: str
    timeout_seconds: float = TIMEOUT_SECONDS
    enable_tls_verification: bool = True


class SuperAdminClientError(Exception):
    """Base exception for Superadmin client errors."""
    pass


class ConnectionError(SuperAdminClientError):
    """Failed to connect to Superadmin API."""
    pass


class AuthenticationError(SuperAdminClientError):
    """Invalid or missing API key."""
    pass


class RateLimitError(SuperAdminClientError):
    """Rate limit exceeded."""
    pass


class TenantNotFoundError(SuperAdminClientError):
    """Tenant does not exist in superadmin system."""
    pass


class PricingSyncClient:
    """Client for syncing pricing from Superadmin system."""
    
    def __init__(self, config: SuperAdminClientConfig):
        self.config = config
        self._client: httpx.AsyncClient | None = None
        
        # Validate configuration
        if not config.base_url.startswith("https://"):
            raise ValueError("Superadmin API URL must use HTTPS")
        if len(config.api_key) < 32:
            raise ValueError("API key must be at least 32 characters")
    
    async def _ensure_client(self) -> httpx.AsyncClient:
        """Lazy initialization of HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.base_url.rstrip("/"),
                timeout=self.config.timeout_seconds,
                verify=self.config.enable_tls_verification,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "VideoReplica-PricingSync/1.0",
                },
            )
        return self._client
    
    async def close(self):
        """Close the HTTP client gracefully."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
    
    @asynccontextmanager
    async def get_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Context manager for client lifecycle."""
        client = await self._ensure_client()
        try:
            yield client
        finally:
            # Don't close here - client is reused across requests
            pass
    
    async def fetch_pricing_snapshot(
        self,
        tenant_id: str,
    ) -> TenantPricingSnapshot | None:
        """Fetch latest pricing for a specific tenant.
        
        Args:
            tenant_id: The tenant identifier to fetch pricing for
            
        Returns:
            TenantPricingSnapshot if successful, None if tenant not found
            
        Raises:
            ConnectionError: Failed to reach Superadmin API
            AuthenticationError: Invalid API key
            RateLimitError: Rate limit exceeded
        """
        url = f"/api/super/pricing/{tenant_id}"
        
        retry_count = 0
        while retry_count < MAX_RETRIES:
            try:
                async with self.get_client() as client:
                    response = await client.get(url)
                    
                    if response.status_code == 404:
                        logger.info(f"Tenant {tenant_id} not found in superadmin system")
                        return None
                    
                    if response.status_code == 401:
                        raise AuthenticationError("Invalid API key or token expired")
                    
                    if response.status_code == 429:
                        retry_after = response.headers.get("Retry-After", "60")
                        wait_time = int(retry_after)
                        logger.warning(
                            f"Rate limited by superadmin API, waiting {wait_time}s"
                        )
                        await asyncio.sleep(wait_time)
                        raise RateLimitError("Rate limit exceeded")
                    
                    if response.status_code != 200:
                        error_msg = response.json().get("detail", f"HTTP {response.status_code}")
                        raise ConnectionError(f"Superadmin API error: {error_msg}")
                    
                    # Parse and validate response
                    try:
                        snapshot_data = response.json()
                        snapshot = TenantPricingSnapshot.model_validate(snapshot_data)
                        
                        logger.debug(
                            f"Fetched pricing for tenant {tenant_id}: "
                            f"video_768p={snapshot.pricing.video_768p}, "
                            f"video_2k={snapshot.pricing.video_2k}, "
                            f"oral={snapshot.pricing.oral}"
                        )
                        
                        return snapshot
                        
                    except ValidationError as e:
                        logger.error(f"Invalid pricing response format: {e}")
                        raise ConnectionError("Malformed response from Superadmin API")
                        
            except httpx.ConnectError as e:
                retry_count += 1
                if retry_count == MAX_RETRIES:
                    logger.error(f"Failed to connect to Superadmin API after {MAX_RETRIES} attempts: {e}")
                    raise ConnectionError(f"Connection failed: {e}")
                
                backoff_ms = BASE_BACKOFF_MS * (2 ** (retry_count - 1))
                await asyncio.sleep(backoff_ms / 1000)
    
    async def check_allowance(self, tenant_id: str) -> AllowanceCheckResponse:
        """Check if a tenant has available allowance for new tasks.
        
        Args:
            tenant_id: The tenant identifier to check
            
        Returns:
            AllowanceCheckResponse indicating if submission is allowed
            
        Raises:
            Same exceptions as fetch_pricing_snapshot
        """
        url = f"/api/super/allowance/{tenant_id}"
        
        try:
            async with self.get_client() as client:
                response = await client.get(url)
                
                if response.status_code != 200:
                    raise ConnectionError(f"Allowance check failed: HTTP {response.status_code}")
                
                try:
                    data = response.json()
                    return AllowanceCheckResponse.model_validate(data)
                except ValidationError as e:
                    raise ConnectionError(f"Invalid allowance response: {e}")
                    
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            # On connection failure, assume allowance is OK to avoid blocking users
            logger.warning(f"Allowance check failed (assuming OK): {e}")
            return AllowanceCheckResponse(
                allowed=True,
                remaining_fen=-1,  # Unknown
                message="Allowance unavailable, proceeding with caution"
            )


# Singleton instance managed by dependency injection
_global_client: PricingSyncClient | None = None


def init_pricing_sync_client(superadmin_api_url: str, api_key: str) -> None:
    """Initialize global pricing sync client from environment variables.
    
    Security: API key should be encrypted using Fernet before storing in env.
    """
    global _global_client
    
    from app.settings import SettingsRepository
    
    # Decrypt API key using Fernet
    settings = SettingsRepository()
    decrypted_key = settings.decrypt_value(api_key)  # Assuming Fernet implementation
    
    config = SuperAdminClientConfig(
        base_url=superadmin_api_url,
        api_key=decrypted_key,
    )
    
    _global_client = PricingSyncClient(config)
    logger.info(f"Initialized PricingSyncClient for {superadmin_api_url}")


async def get_pricing_sync_client() -> PricingSyncClient:
    """Get global pricing sync client instance."""
    if _global_client is None:
        raise RuntimeError(
            "PricingSyncClient not initialized. Call init_pricing_sync_client() at startup."
        )
    return _global_client


async def cleanup_pricing_sync_client():
    """Cleanup global client during shutdown."""
    global _global_client
    if _global_client:
        await _global_client.close()
        _global_client = None
```

### 3.3 Sync Service Module

#### `server/app/tenant_pricing.py`

```python
"""Tenant pricing cache synchronization from superadmin system.

This module provides the core synchronization logic that:
1. Fetches pricing from Superadmin API periodically
2. Validates and transforms pricing data
3. Caches results in local PostgreSQL database
4. Provides fallback to local pricing when sync fails

Performance characteristics:
- Synchronization runs every 5 minutes (configurable)
- Single concurrent sync per tenant (prevents race conditions)
- Degraded performance on Superadmin unavailability (uses cache)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection, sql
from pydantic import ValidationError

from app.db_portable import BusinessConnection
from app.superadmin_client import (
    PricingSyncClient,
    get_pricing_sync_client,
    SuperAdminClientError,
)
from app.tenant_pricing_types import PricingConfig, SyncResult, TenantPricingSnapshot

logger = logging.getLogger(__name__)

# Configuration constants
SYNC_INTERVAL_MINUTES = 5
MAX_AGE_HOURS = 24  # Force refresh if older than this regardless of interval
STALE_THRESHOLD_HOURS = 1  # Consider stale after this without update


class PricingSyncError(Exception):
    """Base exception for pricing sync errors."""
    pass


class SyncConflictError(PricingSyncError):
    """Version conflict during optimistic update."""
    pass


def should_refresh_pricing(conn: BusinessConnection, tenant_id: str) -> bool:
    """Determine if pricing should be refreshed based on freshness policy.
    
    Policy:
    - Refresh if last update > SYNC_INTERVAL_MINUTES ago
    - Force refresh if > MAX_AGE_HOURS regardless of status
    - Warn if > STALE_THRESHOLD_HOURS
    
    Returns:
        True if fresh enough, False if refresh needed
    """
    now = datetime.utcnow()
    
    row = conn.execute(
        """
        SELECT updated_at, version 
        FROM tenant_pricing 
        WHERE tenant_id = %s
        """,
        (tenant_id,),
    ).fetchone()
    
    if row is None:
        return True  # No cache exists, always fetch
    
    updated_at = row["updated_at"]
    version = int(row["version"])
    
    age = now - updated_at
    
    # Always force refresh if older than MAX_AGE
    if age.total_seconds() > MAX_AGE_HOURS * 3600:
        logger.info(f"Forcing refresh for {tenant_id} (age={age})")
        return False
    
    # Warn if getting stale
    if age.total_seconds() > STALE_THRESHOLD_HOURS * 3600:
        logger.warning(f"Pricing for {tenant_id} is stale (age={age})")
    
    # Normal case: check against sync interval
    return age.total_seconds() > SYNC_INTERVAL_MINUTES * 60


def read_pricing(
    conn: BusinessConnection,
    *,
    tenant_id: str = "tenant_platform",
) -> tuple[int, PricingConfig | None]:
    """Read pricing configuration for a tenant.
    
    This is the main entry point used by billing logic throughout the codebase.
    
    Strategy:
    1. Try to read from tenant_pricing cache first
    2. If cache exists and is fresh, use it
    3. If cache is stale, trigger background refresh but return cached value
    4. If cache doesn't exist, fall back to legacy customer_credit_pricing
    
    Args:
        conn: Database connection
        tenant_id: Tenant identifier (defaults to platform tenant)
        
    Returns:
        Tuple of (version, PricingConfig) or (0, None) if no pricing configured
    """
    # First, try to read from tenant_pricing cache
    row = conn.execute(
        """
        SELECT version, config_json 
        FROM tenant_pricing 
        WHERE tenant_id = %s
        """,
        (tenant_id,),
    ).fetchone()
    
    if row and row["config_json"]:
        try:
            pricing_config = PricingConfig.model_validate_json(str(row["config_json"]))
            
            # Check if we need to refresh in background
            if not should_refresh_pricing(conn, tenant_id):
                logger.debug(f"Using fresh cache for {tenant_id}")
                return int(row["version"]), pricing_config
            
            # Schedule background refresh (don't block)
            import asyncio
            from app.superadmin_client import get_pricing_sync_client
            
            try:
                client = asyncio.get_event_loop().run_until_complete(get_pricing_sync_client())
                asyncio.create_task(sync_tenant_pricing_background(client, tenant_id, conn))
            except Exception as e:
                logger.warning(f"Failed to schedule background refresh: {e}")
            
            # Return existing cache anyway (fast path)
            return int(row["version"]), pricing_config
        
        except ValidationError as e:
            logger.error(f"Invalid cached pricing for {tenant_id}: {e}")
            # Fall through to legacy mechanism
    
    # Fallback to legacy customer_credit_pricing
    legacy_row = conn.execute(
        "SELECT version, config_json FROM customer_credit_pricing WHERE id = 1 FOR SHARE"
    ).fetchone()
    
    if legacy_row is None:
        logger.error(f"Legacy pricing config row missing for {tenant_id}")
        return 0, None
    
    if legacy_row["config_json"] is None:
        logger.warning(f"Legacy pricing config is NULL for {tenant_id}")
        return int(legacy_row["version"]), None
    
    try:
        legacy_config = PricingConfig.model_validate_json(str(legacy_row["config_json"]))
        
        # Seed tenant_pricing cache from legacy data
        conn.execute(
            """
            INSERT INTO tenant_pricing (tenant_id, version, config_json, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                config_json = EXCLUDED.config_json,
                updated_at = EXCLUDED.updated_at,
                version = tenant_pricing.version + 1
            """,
            (tenant_id, legacy_row["version"], legacy_row["config_json"], datetime.utcnow()),
            eager=True,
        )
        
        return int(legacy_row["version"]), legacy_config
        
    except ValidationError as e:
        logger.error(f"Invalid legacy pricing for {tenant_id}: {e}")
        return 0, None


async def sync_tenant_pricing_background(
    client: PricingSyncClient,
    tenant_id: str,
    conn: BusinessConnection,
) -> None:
    """Background synchronization of tenant pricing.
    
    This function runs asynchronously and shouldn't block the calling request.
    
    Safety:
    - Uses optimistic locking (version increment)
    - Transaction wrapped (all-or-nothing)
    - Exceptions caught and logged (won't crash worker)
    """
    try:
        result = await sync_tenant_pricing(client, tenant_id, conn)
        
        if result.full_success:
            logger.info(
                f"Successfully synced pricing for {tenant_id}: "
                f"v{result.version_before}→v{result.version_after}"
            )
        elif result.degraded_success:
            logger.warning(
                f"Pricing synced for {tenant_id} but write failed: "
                f"cache may be stale"
            )
        else:
            logger.error(
                f"Failed to sync pricing for {tenant_id}: {result.error_message}"
            )
            
    except Exception as e:
        logger.exception(f"Unexpected error in background sync for {tenant_id}: {e}")


async def sync_tenant_pricing(
    client: PricingSyncClient,
    tenant_id: str,
    conn: BusinessConnection,
) -> SyncResult:
    """Synchronize pricing for a single tenant in a transaction.
    
    This is the core synchronization method called by cron jobs or admin actions.
    
    Algorithm:
    1. Fetch latest pricing from Superadmin API
    2. Validate pricing structure and ranges
    3. Read current version from cache
    4. Write new pricing with version increment (optimistic locking)
    5. Return detailed result for monitoring
    
    Returns:
        SyncResult with full details of operation outcome
    """
    # Step 1: Fetch from Superadmin API
    try:
        snapshot = await client.fetch_pricing_snapshot(tenant_id)
        
        if snapshot is None:
            return SyncResult(
                tenant_id=tenant_id,
                success=False,
                prices_fetched=False,
                db_updated=False,
                version_before=0,
                version_after=0,
                error_message="Tenant not found in Superadmin system"
            )
        
        if snapshot.status != "active":
            return SyncResult(
                tenant_id=tenant_id,
                success=True,  # Successfully fetched
                prices_fetched=True,
                db_updated=False,  # Won't update disabled tenants
                version_before=0,
                version_after=0,
                error_message=f"Tenant {tenant_id} is {snapshot.status}, skipping"
            )
        
    except SuperAdminClientError as e:
        return SyncResult(
            tenant_id=tenant_id,
            success=False,
            prices_fetched=False,
            db_updated=False,
            version_before=0,
            version_after=0,
            error_message=str(e)
        )
    
    # Step 2: Validate pricing configuration
    try:
        pricing = snapshot.pricing
        PricingConfig.model_validate(pricing.model_dump())  # Re-validate
    except ValidationError as e:
        return SyncResult(
            tenant_id=tenant_id,
            success=False,
            prices_fetched=True,
            db_updated=False,
            version_before=0,
            version_after=0,
            error_message=f"Pricing validation failed: {e}"
        )
    
    # Step 3: Prepare data for database write
    pricing_json = pricing.model_dump_json()
    now = datetime.utcnow()
    
    # Step 4: Write to local cache with optimistic locking
    try:
        # Get current version
        current_row = conn.execute(
            "SELECT version FROM tenant_pricing WHERE tenant_id = %s FOR UPDATE",
            (tenant_id,),
        ).fetchone()
        
        version_before = int(current_row["version"]) if current_row else 0
        version_after = version_before + 1
        
        # Insert or update
        conn.execute(
            """
            INSERT INTO tenant_pricing (tenant_id, version, config_json, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                version = EXCLUDED.version,
                config_json = EXCLUDED.config_json,
                updated_at = EXCLUDED.updated_at
            """,
            (tenant_id, version_after, pricing_json, now),
            eager=True,
        )
        
        return SyncResult(
            tenant_id=tenant_id,
            success=True,
            prices_fetched=True,
            db_updated=True,
            version_before=version_before,
            version_after=version_after
        )
        
    except Exception as e:
        logger.error(f"Database write failed for {tenant_id}: {e}")
        return SyncResult(
            tenant_id=tenant_id,
            success=False,
            prices_fetched=True,
            db_updated=False,
            version_before=version_before if 'version_before' in locals() else 0,
            version_after=version_before if 'version_before' in locals() else 0,
            error_message=f"Database error: {e}"
        )


def sync_all_tenants_background() -> None:
    """Trigger background sync for all active tenants.
    
    Called by cron job every 5 minutes.
    Runs parallel for multiple tenants (max 10 concurrent).
    """
    import asyncio
    from app.db_pg import pg_transaction
    from app.superadmin_client import get_pricing_sync_client
    
    async def run_sync():
        client = await get_pricing_sync_client()
        
        # Get list of all active tenants
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            
            rows = conn.execute("""
                SELECT DISTINCT tenant_id 
                FROM users 
                WHERE tenant_id IS NOT NULL 
                ORDER BY tenant_id
            """).fetchall()
            
            tenant_ids = [row["tenant_id"] for row in rows]
        
        # Parallel sync (limit concurrency)
        semaphore = asyncio.Semaphore(10)
        
        async def sync_with_limit(tenant_id: str):
            async with semaphore:
                try:
                    await sync_tenant_pricing(client, tenant_id, conn)
                except Exception as e:
                    logger.error(f"Sync failed for {tenant_id}: {e}")
        
        tasks = [sync_with_limit(tid) for tid in tenant_ids]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    # Run in background
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(run_sync())
        else:
            loop.run_until_complete(run_sync())
    except RuntimeError:
        # No event loop in this thread, create new one
        asyncio.new_event_loop().run_until_complete(run_sync())


# Export public API
__all__ = [
    "read_pricing",
    "sync_all_tenants_background",
    "sync_tenant_pricing",
    "should_refresh_pricing",
]
```

### 3.4 Cron Job Module

#### `server/scripts/sync_tenant_pricing_cron.py`

```python
#!/usr/bin/env python3
"""Cron job for periodic tenant pricing synchronization.

Usage:
    uv run server/scripts/sync_tenant_pricing_cron.py

Intended to be run every 5 minutes via systemd timer or similar:
    [Unit]
    Description=Tenant Pricing Sync Job
    
    [Timer]
    OnCalendar=*:*/5
    Persistent=true
    
    [Install]
    WantedBy=timers.target
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

# Add server root to path
ROOT_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.bootstrap import configure_app_logging
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection
from app.tenant_pricing import sync_tenant_pricing, should_refresh_pricing
from app.superadmin_client import (
    PricingSyncClient,
    init_pricing_sync_client,
    get_pricing_sync_client,
    cleanup_pricing_sync_client,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main():
    """Main entry point for cron job execution."""
    logger.info("=" * 70)
    logger.info("Starting tenant pricing synchronization job")
    logger.info(f"Timestamp: {datetime.utcnow()}")
    
    # Initialize Superadmin client from environment
    superadmin_api_url = "https://superadmin.yourdomain.com"
    api_key_var = "SUPERADMIN_PRICING_API_KEY"
    
    import os
    from app.settings import SettingsRepository
    
    try:
        encrypted_key = os.environ[api_key_var]
        if not encrypted_key:
            logger.error(f"Environment variable {api_key_var} is not set")
            sys.exit(1)
        
        settings = SettingsRepository()
        decrypted_key = settings.decrypt_value(encrypted_key)
        
        init_pricing_sync_client(superadmin_api_url, decrypted_key)
        
    except KeyError:
        logger.error(f"Missing required environment variable: {api_key_var}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to initialize Superadmin client: {e}")
        sys.exit(1)
    
    try:
        # Get all tenants that need pricing sync
        with pg_transaction() as raw:
            conn = BusinessConnection.postgres(raw)
            
            # Find all unique tenant IDs
            tenant_rows = conn.execute("""
                SELECT DISTINCT tenant_id 
                FROM users 
                WHERE tenant_id IS NOT NULL
                ORDER BY tenant_id
            """).fetchall()
            
            tenant_ids = [row["tenant_id"] for row in tenant_rows]
            
            logger.info(f"Found {len(tenant_ids)} tenants to sync: {tenant_ids}")
            
            # Sync each tenant sequentially (idempotent)
            success_count = 0
            fail_count = 0
            
            for tenant_id in tenant_ids:
                try:
                    # Skip if cache is fresh (unless forced refresh)
                    if should_refresh_pricing(conn, tenant_id):
                        result = sync_tenant_pricing(
                            get_pricing_sync_client(),
                            tenant_id,
                            conn,
                        )
                        
                        if result.success:
                            success_count += 1
                            logger.info(
                                f"✓ {tenant_id}: v{result.version_before}→v{result.version_after}"
                            )
                        else:
                            fail_count += 1
                            logger.error(
                                f"✗ {tenant_id}: {result.error_message}"
                            )
                    else:
                        logger.debug(f"~ {tenant_id}: cache fresh, skipping")
                        
                except Exception as e:
                    fail_count += 1
                    logger.exception(f"Exception syncing {tenant_id}: {e}")
            
            # Summary
            logger.info("=" * 70)
            logger.info(f"Synchronization complete:")
            logger.info(f"  Success: {success_count}/{len(tenant_ids)}")
            logger.info(f"  Failed: {fail_count}/{len(tenant_ids)}")
            logger.info(f"  Skipped: {len(tenant_ids) - success_count - fail_count}/{len(tenant_ids)}")
            
            # Exit with appropriate code
            sys.exit(0 if fail_count == 0 else 1)
            
    finally:
        cleanup_pricing_sync_client()


if __name__ == "__main__":
    main()
```

---

## 4. API 契约

### 4.1 Superadmin API Endpoints

#### GET `/api/super/pricing/:tenant_id`

**Request:**
```
GET /api/super/pricing/tenant_aaa
Authorization: Bearer <api_key>
```

**Success Response (200):**
```json
{
  "tenant_id": "tenant_aaa",
  "name": "Example Franchisee",
  "status": "active",
  "pricing": {
    "video_768p": 50000,
    "video_2k": 100000,
    "oral": 1000,
    "points_per_yuan": 100,
    "discount_basis_points": 10000,
    "consumption_rounding": "ceil"
  },
  "effective_from": "2026-09-20T00:00:00Z",
  "expires_at": null,
  "updated_by": "superadmin_001",
  "updated_at": "2026-09-20T12:30:00Z"
}
```

**Error Responses:**
```json
// 404 Not Found
{
  "detail": "Tenant not found"
}

// 403 Forbidden
{
  "detail": "Unauthorized"
}

// 429 Too Many Requests
{
  "detail": "Rate limit exceeded",
  "retry_after": 60
}
```

#### GET `/api/super/allowance/:tenant_id`

**Request:**
```
GET /api/super/allowance/tenant_aaa
Authorization: Bearer <api_key>
```

**Success Response (200):**
```json
{
  "allowed": true,
  "remaining_fen": 500000,
  "credit_limit_fen": 100000,
  "message": null
}
```

### 4.2 API Security Requirements

| 要求 | 实现方式 |
|-----|---------|
| **Transport Security** | TLS 1.2+ mandatory, HSTS enabled |
| **Authentication** | Bearer token (JWT or opaque token) |
| **Authorization** | Token scope must include `pricing:read` |
| **Rate Limiting** | 100 req/min per IP, sliding window |
| **Logging** | All requests logged with correlation ID |

---

## 5. 安全机制

### 5.1 API Key 管理

```python
# server/config/secrets.py (new file)

from __future__ import annotations

import os
from cryptography.fernet import Fernet

class SecretManager:
    """Manages encryption/decryption of sensitive credentials."""
    
    _instance: Fernet | None = None
    
    @classmethod
    def get_instance(cls) -> Fernet:
        """Get or create Fernet instance from environment key."""
        if cls._instance is None:
            key_b64 = os.environ.get("FERNET_MASTER_KEY")
            if not key_b64:
                raise RuntimeError("FERNET_MASTER_KEY not configured")
            
            key = Fernet(key_b64.encode())
            cls._instance = key
            
        return cls._instance
    
    @classmethod
    def encrypt_value(cls, plaintext: str) -> str:
        """Encrypt a string value."""
        fernet = cls.get_instance()
        encrypted = fernet.encrypt(plaintext.encode())
        return encrypted.decode()
    
    @classmethod
    def decrypt_value(cls, ciphertext: str) -> str:
        """Decrypt a string value."""
        fernet = cls.get_instance()
        decrypted = fernet.decrypt(ciphertext.encode())
        return decrypted.decode()


# Usage in superadmin_client.py
def init_pricing_sync_client(superadmin_api_url: str, api_key_env_var: str):
    import os
    from app.config.secrets import SecretManager
    
    encrypted_key = os.environ[api_key_env_var]
    decrypted_key = SecretManager.decrypt_value(encrypted_key)
    
    # Continue initialization...
```

### 5.2 Environment Variables

```bash
# .env.local (or docker-compose.env)

# Superadmin System Configuration
SUPERADMIN_API_URL=https://superadmin.yourdomain.com

# Encrypted API Key (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
FERNET_MASTER_KEY=<your-64-char-base64-key>
SUPERADMIN_PRICING_API_KEY=<encrypted-api-key-here>

# Timeout & Retry Configuration
PRICING_SYNC_TIMEOUT=10
PRICING_SYNC_MAX_RETRIES=3
PRICING_SYNC_INTERVAL_MINUTES=5
```

### 5.3 Audit Logging

```python
# server/app/audit_log.py (append-only logging)

from app.db_portable import BusinessConnection
from datetime import datetime

def log_pricing_sync_event(
    conn: BusinessConnection,
    tenant_id: str,
    success: bool,
    version_before: int,
    version_after: int,
    error_message: str | None = None,
):
    """Append-only audit trail for pricing synchronization."""
    conn.execute("""
        INSERT INTO audit_logs (event_type, tenant_id, actor_user_id, metadata)
        VALUES ('pricing_sync', %s, NULL, %s)
    """, (
        tenant_id,
        {
            "success": success,
            "version_before": version_before,
            "version_after": version_after,
            "error_message": error_message,
            "timestamp": datetime.utcnow().isoformat(),
        },
    ), eager=True)
```

---

## 6. 异常处理与降级策略

### 6.1 故障场景矩阵

| 场景 | 影响 | 降级策略 | 恢复方式 |
|-----|------|---------|---------|
| **Superadmin API 超时** | 无法拉取最新价格 | 使用本地缓存（即使略陈旧） | 自动重试，指数退避 |
| **Superadmin API 返回 500** | 完全不可用 | 使用本地缓存 + 告警 | 人工介入修复 Superadmin |
| **网络中断** | 无法连接 Superadmin | 纯本地模式，永不刷新 | 网络恢复后自动重连 |
| **DB 写入失败** | 缓存未更新但成功拉取 | 下次请求时重新尝试 | 事务回滚不影响后续请求 |
| **版本冲突** | 并发更新同租户 | 乐观锁失败，跳过该租户 | 下次周期重试 |

### 6.2 错误码定义

| 错误码 | HTTP Status | 描述 | 用户可见文案 |
|-------|------------|------|-------------|
| `PRICING_CACHE_MISS` | 404 | 无定价配置 | "服务暂不可用" |
| `PRICING_SYNC_FAILED` | 503 | 同步失败，使用旧缓存 | "系统繁忙，请稍后再试" |
| `TENANT_INACTIVE` | 403 | 租户未激活 | "您的账户已被暂停" |
| `PRICE_EXPIRED` | 410 | 价格已过期 | "请联系客服更新价格配置" |

### 6.3 健康检查接口

```python
# server/app/health.py (add to existing health checks)

from app.tenant_pricing import sync_tenant_pricing, get_pricing_sync_client

async def check_superadmin_connectivity() -> dict:
    """Health check for Superadmin integration."""
    try:
        client = await get_pricing_sync_client()
        # Try fetching platform tenant as probe
        result = await client.fetch_pricing_snapshot("tenant_platform")
        
        if result is None:
            return {
                "status": "degraded",
                "component": "superadmin_api",
                "message": "Tenant not found"
            }
        
        return {
            "status": "healthy",
            "component": "superadmin_api",
            "latency_ms": None  # Will be populated by actual timing
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "component": "superadmin_api",
            "message": str(e)
        }
```

---

## 7. 测试方案

### 7.1 单元测试

#### `server/tests/test_tenant_pricing.py`

```python
"""Comprehensive tests for tenant pricing sync module."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.superadmin_client import (
    PricingSyncClient,
    TenantNotFoundError,
    PricingSyncError,
)
from app.tenant_pricing import (
    PricingConfig,
    SyncResult,
    TenantPricingSnapshot,
    read_pricing,
    should_refresh_pricing,
    sync_tenant_pricing,
)


class TestPricingConfig:
    """Tests for PricingConfig model validation."""
    
    def test_valid_minimal_config(self):
        """Test minimal valid configuration."""
        config = PricingConfig(points_per_yuan=100)
        assert config.video_768p is None
        assert config.points_per_yuan == 100
    
    def test_invalid_price_too_high(self):
        """Reject prices exceeding maximum."""
        with pytest.raises(ValidationError):
            PricingConfig(video_768p=1_000_001)
    
    def test_invalid_discount_range(self):
        """Reject discount outside 0-100% range."""
        with pytest.raises(ValidationError):
            PricingConfig(discount_basis_points=10_001)


class TestTenantPricingSnapshot:
    """Tests for snapshot lifecycle."""
    
    def test_is_expired_with_future_date(self):
        """Future expiry date means not expired."""
        snapshot = TenantPricingSnapshot(
            tenant_id="test",
            name="Test Tenant",
            pricing=PricingConfig(),
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        assert not snapshot.is_expired
    
    def test_is_expired_with_past_date(self):
        """Past expiry date means expired."""
        snapshot = TenantPricingSnapshot(
            tenant_id="test",
            name="Test Tenant",
            pricing=PricingConfig(),
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        assert snapshot.is_expired
    
    def test_is_valid_for_submit_inactive(self):
        """Inactive tenants cannot submit tasks."""
        snapshot = TenantPricingSnapshot(
            tenant_id="test",
            name="Test Tenant",
            pricing=PricingConfig(),
            status="suspended",
        )
        assert not snapshot.is_valid_for_submit


class TestShouldRefreshPricing:
    """Tests for refresh policy enforcement."""
    
    def test_no_cache_exists_always_refresh(self, route_state):
        """First-time load always triggers fetch."""
        with route_state.application_context() as conn:
            assert should_refresh_pricing(conn, "nonexistent_tenant") is True
    
    def test_fresh_cache_skips_fetch(self, route_state):
        """Cache less than 5 minutes old skips fetch."""
        with route_state.application_context() as conn:
            # Insert recent cache
            conn.execute("""
                INSERT INTO tenant_pricing (tenant_id, version, config_json, updated_at)
                VALUES ('fresh_tenant', 1, '{}', %s)
            """, (datetime.utcnow(),), eager=True)
            
            assert should_refresh_pricing(conn, "fresh_tenant") is True  # Still needs initial sync
    
    def test_stale_cache_forces_refresh(self, route_state):
        """Cache older than MAX_AGE hours forces refresh."""
        with route_state.application_context() as conn:
            # Insert very old cache
            old_time = datetime.utcnow() - timedelta(hours=25)
            conn.execute("""
                INSERT INTO tenant_pricing (tenant_id, version, config_json, updated_at)
                VALUES ('stale_tenant', 1, '{}', %s)
            """, (old_time,), eager=True)
            
            assert should_refresh_pricing(conn, "stale_tenant") is False


@pytest.mark.asyncio
class TestSyncTenantPricing:
    """Integration tests for synchronization logic."""
    
    async def test_successful_sync(self, route_state, mock_pricing_client):
        """Happy path: fetch and write succeed."""
        with route_state.application_context() as conn:
            result = await sync_tenant_pricing(mock_pricing_client, "tenant_aaa", conn)
            
            assert result.success is True
            assert result.prices_fetched is True
            assert result.db_updated is True
            assert result.version_after == 1
    
    async def test_tenant_not_found_returns_failure(self, route_state, mock_pricing_client):
        """Tenant missing in Superadmin returns appropriate failure."""
        mock_pricing_client.fetch_pricing_snapshot = AsyncMock(return_value=None)
        
        with route_state.application_context() as conn:
            result = await sync_tenant_pricing(mock_pricing_client, "missing_tenant", conn)
            
            assert result.success is False
            assert result.error_message == "Tenant not found in Superadmin system"
    
    async def test_version_optimistic_locking(self, route_state, mock_pricing_client):
        """Concurrent updates handled via optimistic locking."""
        with route_state.application_context() as conn:
            # Pre-seed with version 10
            conn.execute("""
                INSERT INTO tenant_pricing (tenant_id, version, config_json, updated_at)
                VALUES ('tenant_aaa', 10, '{}', NOW())
            """, eager=True)
            
            result = await sync_tenant_pricing(mock_pricing_client, "tenant_aaa", conn)
            
            assert result.version_before == 10
            assert result.version_after == 11


# Fixtures
@pytest.fixture
def mock_pricing_client():
    """Mock Superadmin client for unit tests."""
    client = AsyncMock(spec=PricingSyncClient)
    
    async def dummy_fetch(tenant_id: str):
        from app.tenant_pricing_types import TenantPricingSnapshot, PricingConfig
        
        return TenantPricingSnapshot(
            tenant_id=tenant_id,
            name=f"Tenant {tenant_id}",
            pricing=PricingConfig(video_768p=50000),
        )
    
    client.fetch_pricing_snapshot = dummy_fetch
    return client
```

### 7.2 端到端测试

```python
# server/tests/test_pricing_e2e.py

"""End-to-end test: Superadmin config → Local sync → Billing usage."""

import pytest

from app.tenant_pricing import read_pricing


def test_full_pricing_flow(route_state):
    """Verify pricing flows correctly through entire stack."""
    # Step 1: Initial state (no cache)
    with route_state.application_context() as conn:
        version, pricing = read_pricing(conn, tenant_id="tenant_platform")
        assert version > 0  # Legacy fallback works
    
    # Step 2: Simulate Superadmin pushes new pricing
    # (In real E2E: call Superadmin API directly)
    
    # Step 3: Trigger sync
    from app.tenant_pricing import sync_all_tenants_background
    sync_all_tenants_background()
    
    # Step 4: Verify cache updated
    with route_state.application_context() as conn:
        version_new, pricing_new = read_pricing(conn, tenant_id="tenant_platform")
        assert version_new > version  # Version incremented
    
    # Step 5: Verify billing uses correct price
    from app.internal_billing import reserve_internal_billing
    # (Would create a task and verify credits calculated correctly)
```

---

## 8. 部署与运维

### 8.1 Docker Compose 配置

```yaml
# docker-compose.override.yml

services:
  video-replica-server:
    environment:
      - SUPERADMIN_API_URL=https://superadmin.yourdomain.com
      - FERNET_MASTER_KEY=${FERNET_MASTER_KEY}
      - SUPERADMIN_PRICING_API_KEY=${ENCRYPTED_API_KEY}
      - PRICING_SYNC_INTERVAL_MINUTES=5
      
    volumes:
      - ./scripts:/app/scripts:ro

  # Cron job container
  pricing-sync-cron:
    image: your-docker-registry/video-replica-server:latest
    command: ["uv", "run", "server/scripts/sync_tenant_pricing_cron.py"]
    environment:
      - SUPERADMIN_API_URL=https://superadmin.yourdomain.com
      - FERNET_MASTER_KEY=${FERNET_MASTER_KEY}
      - SUPERADMIN_PRICING_API_KEY=${ENCRYPTED_API_KEY}
      
    restart: unless-stopped
    scheduling:
      crontab: "*/5 * * * *"  # Every 5 minutes
```

### 8.2 Monitoring Metrics

```python
# server/app/metrics.py (prometheus metrics)

from prometheus_client import Counter, Histogram, Gauge

# Pricing sync metrics
pricing_sync_total = Counter(
    'pricing_sync_total',
    'Total pricing synchronization attempts',
    ['tenant_id', 'success']
)

pricing_sync_duration_seconds = Histogram(
    'pricing_sync_duration_seconds',
    'Time spent on pricing sync',
    ['tenant_id'],
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0]
)

pricing_cache_age_seconds = Gauge(
    'pricing_cache_age_seconds',
    'Age of pricing cache in seconds (0 if fresh)',
    ['tenant_id']
)

# Alert rules for Prometheus/Grafana
# pricing_cache_age_seconds > 300 for 10 minutes → WARNING
# pricing_sync_total{success="false"} > 5 in 5 minutes → CRITICAL
```

### 8.3 Rollout Plan

**Phase 1: Canary (Week 1)**
- Deploy to staging environment
- Enable for 1 internal tenant (tenant_platform)
- Monitor metrics for 7 days

**Phase 2: Limited Production (Week 2)**
- Enable for 5 friendly customers
- A/B test: half with sync, half without
- Gather feedback

**Phase 3: Full Rollout (Week 3)**
- Gradual rollout (10% → 50% → 100%)
- Monitor error rates and latencies
- Kill switch ready (`PRICING_SYNC_ENABLED=false`)

---

## 📝 Appendix

### A. Migration Checklist

- [ ] Execute Alembic migration `0xx_tenant_pricing_cache`
- [ ] Verify initial data seeded for `tenant_platform`
- [ ] Confirm Superadmin API key encrypted in secrets manager
- [ ] Deploy `tenant_pricing.py` + `superadmin_client.py`
- [ ] Start cron job with dry-run mode first
- [ ] Run unit tests: `pytest server/tests/test_tenant_pricing.py`
- [ ] Run E2E tests: `pytest server/tests/test_pricing_e2e.py`
- [ ] Enable monitoring dashboards
- [ ] Document SOP for incident response

### B. Rollback Procedure

If critical issue discovered:

1. **Immediate disable**:
   ```bash
   export PRICING_SYNC_ENABLED=false
   systemctl restart video-replica-server
   ```

2. **Restore previous pricing**:
   ```sql
   DELETE FROM tenant_pricing WHERE tenant_id != 'tenant_platform';
   
   -- Restore legacy pricing via fallback mechanism
   ```

3. **Investigate root cause** → fix → re-deploy

### C. References

- [Original RBAC Design Document](./2026-09-17-超管与加盟商分销架构-design.md)
- [Provider Gateway Implementation](./Viral Billing Transparency Implementation Report.md)
- [PostgreSQL Row Level Security Guide](https://www.postgresql.org/docs/current/sql-createpolicy.html)
- [Pydantic Best Practices](https://docs.pydantic.dev/latest/concepts/)

---

**Document History**

| Version | Date | Author | Changes |
|--------|------|--------|---------|
| V1.0 | 2026-09-20 | Qoder AI | Initial draft for review |

**Next Steps**

1. ✅ Code review from engineering team
2. ✅ Security review for API Key handling
3. ⏳ Implement PoC in feature branch
4. ⏳ Write Superadmin API contracts
5. ⏳ Set up staging environment
