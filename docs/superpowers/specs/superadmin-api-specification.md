# 超管系统 API 设计规范

**日期**: 2026-09-20  
**版本**: V1.0  
**状态**: Draft for Review  
**模块**: Superadmin API Gateway  

---

## 📋 目录

1. [总体设计](#1-总体设计)
2. [API 路由结构](#2-api-路由结构)
3. [认证授权机制](#3-认证授权机制)
4. [核心接口设计](#4-核心接口设计)
5. [错误码规范](#5-错误码规范)
6. [分页与过滤](#6-分页与过滤)
7. [安全与限流](#7-安全与限流)
8. [文档与 OpenAPI](#8-文档与-openapi)

---

## 1. 总体设计

### 1.1 架构原则

| 原则 | 描述 | 实现方式 |
|-----|------|---------|
| **RESTful** | 标准 HTTP 语义 | GET/POST/PUT/PATCH/DELETE + JSON |
| **版本控制** | API 演进不破坏兼容 | `/api/v1/` prefix, future: `/api/v2/` |
| **统一响应格式** | 客户端处理逻辑一致 | `{data, error, pagination}` |
| **幂等性保证** | 重复请求不产生副作用 | Idempotency-Key header support |
| **异步操作** | 长任务不阻塞 | 202 Accepted + `/api/v1/jobs/:id` 轮询 |

### 1.2 技术栈选型

```python
# Core Framework
FastAPI==0.104.1          # Web framework with auto OpenAPI docs
Pydantic==2.5.0           # Data validation & serialization
SQLAlchemy==2.0.23        # ORM (async support)
Alembic==1.13.1           # Database migrations
psycopg==3.1.18           # PostgreSQL driver (async)

# Authentication
python-jose[cryptography]==3.3.0  # JWT tokens
argon2-cffi==23.1.0     # Password hashing

# Utilities
httpx==0.25.2           # Async HTTP client
python-multipart==0.0.6 # Form file uploads
prometheus-client==0.19.0 # Metrics
```

### 1.3 项目结构

```
superadmin-server/
├── app/
│   ├── __init__.py
│   ├── main.py               # Application entry point
│   ├── config.py             # Environment configuration
│   ├── database.py           # DB connection pool
│   │
│   ├── api/                  # API router collection
│   │   ├── v1.py             # Versioned router mount
│   │   ├── routers/
│   │   │   ├── tenants.py    # Tenant management
│   │   │   ├── pricing.py    # Pricing configuration
│   │   │   ├── admins.py     # Admin user CRUD
│   │   │   ├── recharge.py   # Recharge orders
│   │   │   └── profit.py     # Profit analytics
│   │   └── deps.py           # Dependency injections
│   │
│   ├── core/                 # Core business logic
│   │   ├── auth.py           # JWT authentication
│   │   ├── security.py       # Password hashing
│   │   └── exceptions.py     # Custom exceptions
│   │
│   ├── models/               # SQLAlchemy models
│   │   ├── __init__.py
│   │   ├── tenant.py
│   │   ├── admin_user.py
│   │   ├── pricing_config.py
│   │   └── recharge_order.py
│   │
│   ├── schemas/              # Pydantic schemas
│   │   ├── __init__.py
│   │   ├── tenant.py
│   │   ├── pricing.py
│   │   └── response.py
│   │
│   └── services/             # Business service layer
│       ├── __init__.py
│       ├── tenant_service.py
│       ├── pricing_service.py
│       └── recharge_service.py
│
├── alembic/
│   ├── versions/             # Migration scripts
│   └── env.py
│
├── tests/
│   ├── conftest.py
│   ├── test_tenants.py
│   ├── test_pricing.py
│   └── fixtures/
│
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml
```

---

## 2. API 路由结构

### 2.1 路由表

| 资源 | 路径 | 方法 | 权限 | 描述 |
|-----|------|------|------|------|
| **Tenants** |||||
| List tenants | `/api/v1/tenants` | GET | super_admin | 查看所有租户 |
| Get tenant | `/api/v1/tenants/{tenant_id}` | GET | super_admin | 查看单个租户详情 |
| Create tenant | `/api/v1/tenants` | POST | super_admin | 创建新加盟商 |
| Update tenant | `/api/v1/tenants/{tenant_id}` | PATCH | super_admin | 修改租户信息 |
| Suspend tenant | `/api/v1/tenants/{tenant_id}/suspend` | POST | super_admin | 暂停/启用租户 |
| Delete tenant | `/api/v1/tenants/{tenant_id}` | DELETE | super_admin | 删除租户（软删除） |
| **Pricing** |||||
| Set pricing | `/api/v1/pricing/{tenant_id}` | PUT | super_admin | 设置该租户零售价 |
| Get pricing | `/api/v1/pricing/{tenant_id}` | GET | super_admin | 获取租户定价快照 |
| Get all pricing | `/api/v1/pricing` | GET | super_admin | 批量导出所有定价 |
| Bulk update pricing | `/api/v1/pricing/bulk` | POST | super_admin | 批量更新（CSV import） |
| **Allowance** |||||
| Check allowance | `/api/v1/allowance/{tenant_id}` | GET | public_key | 查询可用额度（供 Client 调用） |
| Reserve allowance | `/api/v1/allowance/reserve` | POST | public_key | 预占额度（用于 task submit） |
| Release allowance | `/api/v1/allowance/release` | POST | public_key | 释放未使用的额度 |
| **Admin Users** |||||
| List admins | `/api/v1/admins` | GET | super_admin | 查看所有管理员 |
| Create admin | `/api/v1/admins` | POST | super_admin | 创建加盟商主账号 |
| Update admin | `/api/v1/admins/{admin_id}` | PATCH | super_admin | 修改管理员信息 |
| Reset password | `/api/v1/admins/{admin_id}/reset-password` | POST | super_admin | 签发一次性恢复凭据 |
| Change role | `/api/v1/admins/{admin_id}/role` | PATCH | super_admin | 更改角色（admin ↔ employee） |
| **Recharge Orders** |||||
| List orders | `/api/v1/recharge-orders` | GET | super_admin | 查看充值订单 |
| Get order details | `/api/v1/recharge-orders/{order_id}` | GET | super_admin | 订单详情 |
| Refund order | `/api/v1/recharge-orders/{order_id}/refund` | POST | super_admin | 退款操作 |
| **Profit Analytics** |||||
| Get platform profit | `/api/v1/profit/platform` | GET | super_admin | 平台总利润统计 |
| Get tenant profit | `/api/v1/profit/{tenant_id}` | GET | super_admin | 单租户利润详情 |
| Export profit report | `/api/v1/profit/export` | POST | super_admin | 导出财务报表（CSV） |
| **System Settings** |||||
| Get provider settings | `/api/v1/settings/providers` | GET | super_admin | 查看上游服务商配置 |
| Update provider settings | `/api/v1/settings/providers` | PUT | super_admin | 更新服务商密钥 |
| Get upstream cost | `/api/v1/settings/upstream-cost` | GET | super_admin | 查看真实成本 |
| Update upstream cost | `/api/v1/settings/upstream-cost` | PUT | super_admin | 更新上游成本 |

---

## 3. 认证授权机制

### 3.1 认证类型

#### **Type A: OAuth2 Access Token (Web Admin Portal)**

```python
# Authentication flow
POST /api/v1/auth/login
{
  "username": "superadmin@example.com",
  "password": "strong_password"
}

Response (200 OK):
{
  "access_token": "<JWT·示例占位>...",
  "token_type": "bearer",
  "expires_in": 3600,
  "refresh_token": "dGhpcyBpcyBhIHJlZnJlc2ggdG9rZW4..."
}

# Use in subsequent requests
Authorization: Bearer <access_token>
```

**Token Claims:**
```json
{
  "sub": "admin_user_uuid",
  "email": "superadmin@example.com",
  "role": "super_admin",
  "tenant_id": null,  // super_admin has no tenant
  "exp": 1695234567,
  "iat": 1695230967,
  "jti": "unique-token-id"
}
```

#### **Type B: API Key (Client Systems)**

```python
# Generate API key for client system
POST /api/v1/admins/{admin_id}/api-keys
{
  "name": "Video Replica Client Sync",
  "scopes": ["pricing:read", "allowance:check"],
  "expires_at": "2026-12-31T23:59:59Z"
}

Response (201 Created):
{
  "api_key_id": "ak_abc123def456",
  "key": "sk_live_<仅创建时返回一次·示例占位>",  // Only shown once!
  "name": "Video Replica Client Sync",
  "scopes": ["pricing:read", "allowance:check"],
  "created_at": "2026-09-20T10:00:00Z",
  "expires_at": "2026-12-31T23:59:59Z"
}

# Use in client requests
GET /api/v1/pricing/tenant_aaa
X-API-Key: sk_live_<仅创建时返回一次·示例占位>
```

**Scopes Definition:**
| Scope | Permission | Description |
|-------|-----------|-------------|
| `pricing:read` | Read | Can query pricing configs |
| `pricing:write` | Write | Can modify pricing (super_admin only) |
| `allowance:check` | Read | Can query tenant allowance |
| `allowance:reserve` | Write | Can reserve/release allowances |
| `recharge:read` | Read | Can view recharge orders |
| `profit:read` | Read | Can access profit reports |

#### **Type C: Mutual TLS (Internal Services)**

```bash
# For server-to-server communication within trusted network
curl --cert /path/to/client.crt --key /path/to/client.key \
     https://superadmin.yourdomain.com/api/v1/internal/pricing/tenant_aaa
```

### 3.2 授权中间件

```python
# app/api/deps.py

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from typing import Annotated

security = HTTPBearer()

class RequiredScope:
    def __init__(self, scopes: list[str]):
        self.scopes = scopes
    
    async def __call__(self, credentials: HTTPAuthorizationCredentials = Depends(security)):
        token = credentials.credentials
        
        try:
            payload = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=["HS256"]
            )
            
            user_scopes = set(payload.get("scopes", []))
            if not all(scope in user_scopes for scope in self.scopes):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Insufficient permissions",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            
            return payload
            
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

# Usage
async def get_current_super_admin(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token = credentials.credentials
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    
    if payload.get("role") != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin role required"
        )
    
    return payload

# In router
@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(get_current_super_admin),
):
    ...
```

---

## 4. 核心接口设计

### 4.1 Tenant Management (tenants.py)

```python
# app/api/routers/tenants.py

from fastapi import APIRouter, Query, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional

from app.database import get_db
from app.schemas.tenant import TenantCreate, TenantUpdate, TenantResponse
from app.services.tenant_service import TenantService
from app.core.auth import get_current_super_admin

router = APIRouter(prefix="/tenants", tags=["Tenants"])


@router.get("", response_model=List[TenantResponse], summary="List all tenants")
async def list_tenants(
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=500, description="Max records to return"),
    search: Optional[str] = Query(None, description="Search by name or ID"),
    status: Optional[Literal["active", "suspended", "closed"]] = Query(None),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    List all franchisee tenants with pagination and filtering.
    
    - **skip**: Number of records to skip (for pagination)
    - **limit**: Maximum number of records to return (max 500)
    - **search**: Filter by tenant name or ID substring match
    - **status**: Filter by tenant status (active/suspended/closed)
    """
    service = TenantService(db)
    tenants = await service.list_tenants(
        skip=skip,
        limit=limit,
        search=search,
        status=status,
    )
    return tenants


@router.post("", response_model=TenantResponse, status_code=201, summary="Create a new tenant")
async def create_tenant(
    tenant_data: TenantCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Create a new franchisee tenant.
    
    Creates:
    - New tenant record
    - Associated wallet balance (initially 0)
    - Default owner admin user account
    """
    service = TenantService(db)
    
    # Validate that last super_admin won't be deleted
    if tenant_data.owner_admin_reset_required:
        count = await service.count_active_super_admins()
        if count <= 1:
            raise HTTPException(
                status_code=400,
                detail="LAST_SUPER_ADMIN_PROTECTED"
            )
    
    tenant = await service.create_tenant(tenant_data)
    return tenant


@router.get("/{tenant_id}", response_model=TenantResponse, summary="Get tenant details")
async def get_tenant(
    tenant_id: str,
    include_pricing: bool = Query(True, description="Include pricing configuration"),
    include_wallet: bool = Query(True, description="Include wallet balance"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Retrieve detailed information about a single tenant.
    
    Includes:
    - Basic tenant info (name, status, created_at)
    - Wallet balance and credit limit
    - Pricing configuration (if requested)
    - Recent activity log (last 10 actions)
    """
    service = TenantService(db)
    tenant = await service.get_tenant_by_id(
        tenant_id,
        include_pricing=include_pricing,
        include_wallet=include_wallet,
    )
    
    if not tenant:
        raise HTTPException(
            status_code=404,
            detail=f"Tenant {tenant_id} not found"
        )
    
    return tenant


@router.patch("/{tenant_id}", response_model=TenantResponse, summary="Update tenant info")
async def update_tenant(
    tenant_id: str,
    update_data: TenantUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Partially update tenant information.
    
    Allowed fields:
    - name
    - metadata
    - notes
    """
    service = TenantService(db)
    tenant = await service.update_tenant(tenant_id, update_data)
    return tenant


@router.post("/{tenant_id}/suspend", summary="Suspend or reactivate tenant")
async def toggle_tenant_status(
    tenant_id: str,
    action: Literal["suspend", "activate"],
    reason: str = Query(..., description="Reason for suspension"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Suspend or activate a tenant.
    
    Suspended tenants cannot:
    - Submit new tasks
    - Make purchases
    
    But can:
    - View existing data
    - Download generated assets
    """
    service = TenantService(db)
    
    result = await service.toggle_tenant_status(
        tenant_id,
        action=action,
        reason=reason,
        updated_by=current_user["sub"],
    )
    
    return {"success": True, "message": f"Tenant {tenant_id} {action}d"}


@router.delete("/{tenant_id}", status_code=204, summary="Soft delete tenant")
async def delete_tenant(
    tenant_id: str,
    confirm_delete: bool = Query(False, description="Must explicitly confirm"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Soft-delete a tenant (marks as 'closed' instead of physical deletion).
    
    WARNING: This operation is irreversible unless you have database backups.
    
    Must explicitly confirm with `?confirm_delete=true`.
    """
    if not confirm_delete:
        raise HTTPException(
            status_code=400,
            detail="Must confirm deletion with ?confirm_delete=true"
        )
    
    service = TenantService(db)
    await service.close_tenant(tenant_id, closed_by=current_user["sub"])
    
    return None  # 204 No Content
```

### 4.2 Pricing Configuration (pricing.py)

```python
# app/api/routers/pricing.py

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from typing import List, Dict

from app.database import get_db
from app.schemas.pricing import (
    PricingConfig, 
    PricingSnapshot, 
    BulkPricingUpdate,
    PricingExportResponse
)
from app.services.pricing_service import PricingService
from app.core.auth import get_current_super_admin

router = APIRouter(prefix="/pricing", tags=["Pricing"])


@router.put("/{tenant_id}", response_model=PricingSnapshot, summary="Set pricing for tenant")
async def set_pricing(
    tenant_id: str,
    pricing: PricingConfig,
    effective_from: str = Query(None, description="Effective timestamp (ISO8601)"),
    expires_at: str = Query(None, description="Expiration timestamp (ISO8601)"),
    note: str = Query(None, description="Change note for audit trail"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Configure or update pricing for a specific tenant.
    
    Pricing subjects:
    - video_768p: Price per generation task (in fen)
    - video_2k: Price per 2K generation task (in fen)
    - oral: Price per second of avatar speech (in fen)
    - points_per_yuan: Conversion rate (default: 100 points/RMB)
    - discount_basis_points: Discount percentage (10000 = 100%)
    
    All prices are in fen (1 RMB = 100 fen).
    """
    service = PricingService(db)
    
    snapshot = await service.set_pricing(
        tenant_id=tenant_id,
        pricing_config=pricing,
        effective_from=effective_from,
        expires_at=expires_at,
        note=note,
        updated_by=current_user["sub"],
    )
    
    return snapshot


@router.get("/{tenant_id}", response_model=PricingSnapshot, summary="Get pricing snapshot")
async def get_pricing_snapshot(
    tenant_id: str,
    as_of: str = Query(None, description="Retrieve historical pricing at timestamp"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Retrieve current or historical pricing configuration.
    
    If `as_of` provided, returns pricing active at that timestamp.
    Otherwise returns latest active pricing.
    """
    service = PricingService(db)
    
    from datetime import datetime
    as_of_dt = datetime.fromisoformat(as_of) if as_of else None
    
    snapshot = await service.get_pricing_snapshot(
        tenant_id=tenant_id,
        as_of=as_of_dt,
    )
    
    if not snapshot:
        raise HTTPException(
            status_code=404,
            detail=f"No pricing history found for tenant {tenant_id}"
        )
    
    return snapshot


@router.get("", response_model=Dict[str, PricingSnapshot], summary="Get all pricing snapshots")
async def list_all_pricing(
    active_only: bool = Query(True, description="Only active (not expired) configurations"),
    tenant_ids: List[str] = Query(None, description="Filter by specific tenant IDs"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Bulk retrieve pricing configurations for multiple tenants.
    
    Returns:
    {
      "tenant_aaa": { ... },
      "tenant_bbb": { ... },
      ...
    }
    """
    service = PricingService(db)
    pricing_map = await service.list_all_pricing(
        active_only=active_only,
        tenant_ids=tenant_ids,
    )
    return pricing_map


@router.post("/bulk", response_model=Dict[str, str], summary="Bulk update pricing via CSV")
async def bulk_update_pricing(
    file: UploadFile = File(..., description="CSV file with pricing data"),
    dry_run: bool = Query(False, description="Validate without committing"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Bulk update pricing configurations using CSV import.
    
    CSV format:
    ```csv
    tenant_id,video_768p,video_2k,oral,points_per_yuan,note
    tenant_aaa,50000,100000,1000,100,Updated Q4 rates
    tenant_bbb,45000,90000,900,100,Discounted package
    ```
    
    Dry-run mode validates format but doesn't commit changes.
    """
    import csv
    from io import StringIO
    
    content = await file.read()
    decoder = StringIO(content.decode('utf-8'))
    reader = csv.DictReader(decoder)
    
    updates = []
    errors = []
    
    for row_num, row in enumerate(reader, start=2):  # Start at 2 (skip header)
        try:
            update = BulkPricingUpdate.model_validate(row)
            updates.append(update)
        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")
    
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"Parsing errors: {'; '.join(errors[:5])}"  # Limit to first 5
        )
    
    results = {}
    if not dry_run:
        service = PricingService(db)
        
        for update in updates:
            try:
                await service.set_pricing(
                    tenant_id=update.tenant_id,
                    pricing_config=PricingConfig(**{
                        k: v for k, v in update.dict().items()
                        if k in ['video_768p', 'video_2k', 'oral', 'points_per_yuan']
                    }),
                    note=update.note,
                    updated_by=current_user["sub"],
                )
                results[update.tenant_id] = "success"
            except Exception as e:
                results[update.tenant_id] = f"error: {str(e)}"
    
    return results


@router.get("/export", response_model=PricingExportResponse, summary="Export pricing report")
async def export_pricing_report(
    format: str = Query("csv", enum=["csv", "xlsx"], description="Export format"),
    include_history: bool = Query(False, description="Include all historical snapshots"),
    date_range_start: str = Query(None, description="Start date (YYYY-MM-DD)"),
    date_range_end: str = Query(None, description="End date (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    """
    Export comprehensive pricing report for auditing/accounting purposes.
    
    Includes:
    - Current active pricing for all tenants
    - Historical changes (if requested)
    - Audit trail (who changed what and when)
    """
    service = PricingService(db)
    
    export_data = await service.export_pricing_report(
        format=format,
        include_history=include_history,
        date_range=(date_range_start, date_range_end),
    )
    
    return export_data
```

### 4.3 Allowance Check (public endpoint for Client System)

```python
# app/api/routers/allowance.py

from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.allowance import AllowanceCheckResponse
from app.services.allowance_service import AllowanceService
from app.core.exceptions import AppErrorCodes

router = APIRouter(prefix="/allowance", tags=["Allowance"])


@router.get("/{tenant_id}", response_model=AllowanceCheckResponse, 
            summary="Check tenant allowance",
            openapi_extra={
                "x-api-key-auth": True  # Mark as requiring API key auth
            })
async def check_allowance(
    tenant_id: str,
    x_api_key: str = Header(..., description="Public API key for Video Replica client"),
    check_pending_tasks: bool = Query(True, description="Include pending task commitments"),
    db: Session = Depends(get_db),
):
    """
    Check available allowance for a tenant.
    
    This endpoint is called by the Video Replica client BEFORE task submission.
    
    Response includes:
    - allowed: Boolean indicating if new tasks can be submitted
    - remaining_fen: Available balance in fen
    - credit_limit_fen: Extended credit limit (can go negative up to this)
    
    If allowed=false, clients should show:
    "服务暂不可用，请联系客服" (do NOT expose internal欠费 details)
    """
    # Verify API key
    service = AllowanceService(db)
    
    # Check if API key is valid and has appropriate scope
    if not await service.validate_api_key(x_api_key, scope="allowance:check"):
        raise HTTPException(
            status_code=403,
            detail="INVALID_API_KEY"
        )
    
    # Get allowance
    allowance = await service.get_tenant_allowance(
        tenant_id=tenant_id,
        check_pending_tasks=check_pending_tasks,
    )
    
    if not allowance:
        raise HTTPException(
            status_code=404,
            detail=f"Tenant {tenant_id} not found"
        )
    
    # Transform to external-facing format (hide internal details)
    return AllowanceCheckResponse(
        allowed=allowance.can_submit_new_tasks,
        remaining_fen=allowance.available_balance_fen,
        credit_limit_fen=allowance.credit_limit_fen,
        message=None if allowance.can_submit_new_tasks else "INSUFFICIENT_BALANCE",
    )


@router.post("/reserve", response_model=dict, summary="Reserve allowance temporarily")
async def reserve_allowance(
    reservation: Dict,  # {tenant_id, amount_fen, task_id, expires_minutes}
    x_api_key: str = Header(...),
    db: Session = Depends(get_db),
):
    """
    Temporarily reserve allowance for an upcoming task.
    
    This creates a hold on funds that lasts until:
    - Task completes (commit/release)
    - Timeout (auto-release)
    - Explicit release call
    """
    service = AllowanceService(db)
    
    reservation_id = await service.reserve_allowance(
        **reservation,
        api_key=x_api_key,
    )
    
    return {"reservation_id": reservation_id}


@router.post("/release", response_model=dict, summary="Release reserved allowance")
async def release_allowance(
    reservation_id: str,
    committed: bool = Query(True, description="Was task actually charged?"),
    amount_fen: int = Query(None, description="Actual charged amount (overrides reservation)"),
    x_api_key: str = Header(...),
    db: Session = Depends(get_db),
):
    """
    Release a previously reserved allowance.
    
    If committed=true, also deduct the actual amount from balance.
    If committed=false, simply release the hold.
    """
    service = AllowanceService(db)
    
    await service.release_allowance(
        reservation_id=reservation_id,
        committed=committed,
        amount_fen=amount_fen,
    )
    
    return {"released": True}
```

### 4.4 AllowanceCheckResponse Schema

```python
# app/schemas/allowance.py

from pydantic import BaseModel, Field
from typing import Optional

class AllowanceCheckResponse(BaseModel):
    """Response from allowance check API."""
    
    allowed: bool = Field(
        ...,
        description="Whether tenant can submit new tasks"
    )
    remaining_fen: int = Field(
        ...,
        description="Available balance in fen (minimum 0)"
    )
    credit_limit_fen: Optional[int] = Field(
        default=None,
        description="Extended credit limit (can go negative up to this)"
    )
    message: Optional[str] = Field(
        default=None,
        description="Human-readable message (internal codes only)"
    )
```

---

## 5. 错误码规范

### 5.1 统一错误响应格式

```json
{
  "error": {
    "code": "TENANT_NOT_FOUND",
    "message": "Tenant with ID 'tenant_xxx' does not exist",
    "details": {
      "tenant_id": "tenant_xxx"
    },
    "timestamp": "2026-09-20T12:34:56Z",
    "request_id": "req_abc123def456"
  }
}
```

### 5.2 错误码分类表

| Category | Code Range | HTTP Status | Example |
|----------|-----------|------------|---------|
| **Auth Errors** | 1000-1099 | 401 | `AUTH_INVALID_TOKEN` |
| **Permission Errors** | 1100-1199 | 403 | `SCOPE_INSUFFICIENT` |
| **Validation Errors** | 2000-2099 | 400 | `PRICE_TOO_HIGH`, `TENANT_NAME_REQUIRED` |
| **Resource Errors** | 3000-3099 | 404 | `TENANT_NOT_FOUND`, `PRICING_CONFIG_MISSING` |
| **Business Logic Errors** | 4000-4999 | 402/409 | `LAST_SUPER_ADMIN_PROTECTED`, `TENANT_SUSPENDED` |
| **Rate Limit Errors** | 5000-5099 | 429 | `RATE_LIMIT_EXCEEDED` |
| **System Errors** | 9000-9099 | 500 | `DATABASE_CONNECTION_FAILED` |

### 5.3 核心业务错误码

```python
# app/core/exceptions.py

from enum import Enum
from fastapi import HTTPException, status

class ErrorCode(Enum):
    # Authentication (1xxx)
    AUTH_INVALID_TOKEN = (1001, "Invalid or expired authentication token")
    AUTH_MFA_REQUIRED = (1002, "Multi-factor authentication required")
    
    # Permission (11xx)
    SCOPE_INSUFFICIENT = (1101, "API key lacks required permission scope")
    TENANT_ACCESS_DENIED = (1102, "Access denied to this tenant's resources")
    
    # Validation (2xxx)
    PRICE_TOO_HIGH = (2001, "Price exceeds maximum allowed value")
    PRICE_INVALID_FORMAT = (2002, "Price must be positive integer in fen")
    TENANT_NAME_REQUIRED = (2003, "Tenant name is required")
    EMAIL_INVALID = (2004, "Invalid email format")
    
    # Resource (3xxx)
    TENANT_NOT_FOUND = (3001, "Tenant with specified ID does not exist")
    ADMIN_USER_NOT_FOUND = (3002, "Admin user not found")
    PRICING_CONFIG_MISSING = (3003, "No pricing configuration exists")
    API_KEY_NOT_FOUND = (3004, "API key not found or revoked")
    
    # Business Logic (4xxx)
    LAST_SUPER_ADMIN_PROTECTED = (4001, "Cannot delete or demote the last super admin")
    TENANT_SUSPENDED = (4002, "Tenant account is suspended")
    TENANT_CLOSED = (4003, "Tenant account has been closed")
    INSUFFICIENT_ALLOWANCE = (4004, "Insufficient balance or credit limit")
    PRICING_EXPIRED = (4005, "Pricing configuration has expired")
    
    # Rate Limit (5xxx)
    RATE_LIMIT_EXCEEDED = (5001, "Too many requests. Please retry after {retry_after}s")
    
    # System (9xxx)
    DATABASE_ERROR = (9001, "Database operation failed")
    CACHE_UNAVAILABLE = (9002, "Cache service temporarily unavailable")

class AppHTTPException(HTTPException):
    """Custom exception with error code metadata."""
    
    def __init__(self, error_code: ErrorCode, detail: str | None = None, **kwargs):
        http_detail = detail or error_code.value[1].format(**kwargs) if kwargs else error_code.value[1]
        
        status_map = {
            1001: status.HTTP_401_UNAUTHORIZED,
            1101: status.HTTP_403_FORBIDDEN,
            2001: status.HTTP_400_BAD_REQUEST,
            3001: status.HTTP_404_NOT_FOUND,
            4001: status.HTTP_409_CONFLICT,
            4002: status.HTTP_403_FORBIDDEN,
            4004: status.HTTP_402_PAYMENT_REQUIRED,
            5001: status.HTTP_429_TOO_MANY_REQUESTS,
            9001: status.HTTP_500_INTERNAL_SERVER_ERROR,
        }
        
        super().__init__(
            status_code=status_map.get(error_code.value[0], status.HTTP_500_INTERNAL_SERVER_ERROR),
            detail={
                "error": {
                    "code": error_code.name,
                    "message": http_detail,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            }
        )
```

---

## 6. 分页与过滤

### 6.1 标准化分页响应

```python
# app/schemas/response.py

from pydantic import BaseModel, Field
from typing import Generic, TypeVar, Optional

T = TypeVar('T')

class PaginationMeta(BaseModel):
    total: int = Field(..., description="Total number of records")
    skip: int = Field(..., description="Number of records skipped")
    limit: int = Field(..., description="Number of records returned")
    has_more: bool = Field(..., description="Whether more records exist")


class PaginatedResponse(BaseModel, Generic[T]):
    data: list[T] = Field(..., description="List of items")
    meta: PaginationMeta = Field(..., description="Pagination metadata")
    
    @classmethod
    def from_query(cls, items: list[T], total: int, skip: int, limit: int):
        return cls(
            data=items,
            meta=PaginationMeta(
                total=total,
                skip=skip,
                limit=limit,
                has_more=skip + limit < total,
            )
        )


# Usage in router
@router.get("/tenants", response_model=PaginatedResponse[TenantResponse])
async def list_tenants(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_super_admin),
):
    tenants, total = await TenantService.list_with_count(db, skip, limit)
    return PaginatedResponse.from_query(
        items=[TenantResponse.from_orm(t) for t in tenants],
        total=total,
        skip=skip,
        limit=limit,
    )
```

### 6.2 高级过滤语法

```python
# Support filter query params like:
# /api/v1/tenants?status=active&created_before=2026-09-01&name_icontains=villa

from fastapi import Query
from datetime import datetime
from typing import Optional, List

class TenantFilters(BaseModel):
    status: Optional[str] = Query(None, pattern="^(active|suspended|closed)$")
    name_icontains: Optional[str] = Query(None, description="Case-insensitive substring match")
    created_after: Optional[datetime] = Query(None)
    created_before: Optional[datetime] = Query(None)
    min_wallet_balance: Optional[int] = Query(None, description="Minimum balance in fen")
    max_wallet_balance: Optional[int] = Query(None)
```

---

## 7. 安全与限流

### 7.1 速率限制策略

```python
# app/core/ratelimit.py

from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request

limiter = Limiter(key_func=get_remote_address)

# Rate limits per IP address
LIMITS = {
    "default": "100 per minute",
    "auth": "10 per minute",  # Login attempts
    "pricing_sync": "20 per minute",  # Client sync calls
    "export": "5 per hour",  # Heavy export operations
}

# Handle rate limit exceeded
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": f"Too many requests. Please retry after {exc.limit.amount} seconds",
                "retry_after": exc.limit.amount,
            }
        }
    )
```

### 7.2 CORS 配置

```python
# app/config/cors.py

from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://admin.yourdomain.com",  # Admin portal
        "https://client-video-replica.example.com",  # Client systems
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=3600,  # Cache preflight results for 1 hour
)
```

### 7.3 审计日志

```python
# app/middleware/audit.py

from fastapi import Request
import json

class AuditLoggerMiddleware:
    """Log all sensitive operations to append-only audit table."""
    
    async def __call__(self, request: Request, call_next):
        # Only log non-GET methods (writes)
        if request.method not in ["GET", "HEAD", "OPTIONS"]:
            path = request.url.path
            body = await request.json() if request.headers.get("content-type") == "application/json" else {}
            
            # Log to audit log
            audit_entry = {
                "timestamp": datetime.utcnow(),
                "method": request.method,
                "path": path,
                "user_id": request.state.user_id if hasattr(request.state, 'user_id') else None,
                "ip": request.client.host if request.client else None,
                "body": body,
            }
            
            await log_to_audit_table(audit_entry)
        
        response = await call_next(request)
        return response
```

---

## 8. 文档与 OpenAPI

### 8.1 Auto-generated OpenAPI Spec

```python
# app/main.py

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

app = FastAPI(
    title="Superadmin API",
    description="""
## 超管系统后端 API 文档

支持的功能：
- 加盟商（租户）生命周期管理
- 零售价配置与同步
- 充值订单管理
- 利润报表分析
- 服务商配置

### 认证
使用 Bearer Token（JWT）或 API Key 进行认证。详见「认证授权机制」章节。
    """,
    version="1.0.0",
    contact={
        "name": "API Support",
        "email": "api-support@yourcompany.com",
    },
)

# Mount API routers
from app.api.v1 import router as v1_router
app.include_router(v1_router, prefix="/api/v1")

# Custom OpenAPI schema
@app.get("/openapi.json", include_in_schema=False)
async def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    
    # Add security scheme
    openapi_schema["components"] = {
        "securitySchemes": {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            },
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
            },
        }
    }
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema
```

### 8.2 Swagger UI 自定义样式

Embed Swagger UI with custom branding:

```html
<!-- templates/swagger-ui.html -->
<!DOCTYPE html>
<html>
<head>
    <title>Superadmin API Docs</title>
    <link rel="icon" href="/favicon.ico" />
    <style>
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        .swagger-ui .topbar {
            background-color: transparent;
            background-image: url('/static/header-bg.png');
        }
    </style>
</head>
<body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@4/swagger-ui-bundle.js"></script>
    <script src="https://unpkg.com/swagger-ui-dist@4/swagger-ui-standalone-preset.js"></script>
    <script>
        window.onload = function() {
            SwaggerUIBundle({
                url: "/openapi.json",
                dom_id: '#swagger-ui',
                presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
                syntaxHighlight: {
                    activated: true,
                    theme: {
                        "atom-one-dark": require("prismjs/themes/prismAtomOneDark.css")
                    }
                }
            });
        };
    </script>
</body>
</html>
```

---

## 📝 Appendix

### A. API 变更历史

| 版本 | 日期 | 变更说明 |
|-----|------|---------|
| V1.0 | 2026-09-20 | Initial release |

### B. 参考文档

- [FastAPI Official Documentation](https://fastapi.tiangolo.com/)
- [OpenAPI Specification](https://spec.openapis.org/oas/v3.1.0)
- [RESTful API Design Best Practices](https://restfulapi.net/)
- [JSON:API Format](https://jsonapi.org/)

### C. 快速启动

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Access Swagger UI at http://localhost:8000/docs
```

---

**文档维护**：此文档应随 API 变更实时更新。任何新增 endpoint 必须在 Swagger UI 中自动展示。
