"""Superadmin Server - FastAPI application entry point.

This module provides the main FastAPI application instance with:
- CORS middleware for cross-origin requests
- Exception handlers for consistent error responses
- API router mounting (v1 endpoints)
- Prometheus metrics endpoint

Author: AI Implementation Team
Date: 2026-09-20
Worktree: feat/admin-rbac-superadmin-20260917
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from app.api.v1 import router as v1_router
from app.config.settings import settings
from app.core.exceptions import HTTPExceptionHandlers
from app.database import init_db
from app.middleware.audit import AuditLoggerMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle manager.
    
    This function runs once at server start and once at server shutdown.
    
    Startup sequence:
    1. Initialize database connection pool
    2. Print startup log with configuration summary
    
    Shutdown sequence:
    1. Graceful cleanup of resources
    2. Connection pool termination
    """
    # Startup phase
    await init_db()
    print(f"🚀 Superadmin API starting with config:")
    print(f"   Database: {settings.database_url}")
    print(f"   JWT Secret: {'***' * 8 if settings.jwt_secret else 'NOT SET'}")
    print(f"   Superadmin API URL: {settings.superadmin_api_url}")
    
    yield
    
    # Shutdown phase
    print("👋 Superadmin API shutting down...")


# Create FastAPI application instance
app = FastAPI(
    title="Superadmin API",
    description="""
## 超管系统后端 API 接口文档

### 功能概述

本系统支持以下核心功能：

#### 1. 加盟商（租户）生命周期管理
- 创建/启用/停用/删除加盟商账号
- 查看加盟商详细信息和状态
- 批量操作和导入导出

#### 2. 零售价配置与同步
- 为每个加盟商设置零售价（视频/口播）
- 批量更新价格配置（CSV 导入）
- 历史价格快照查询
- 自动同步到客户端系统

#### 3. 充值订单管理
- 查看所有充值订单记录
- 退款审批流程
- 订单状态追踪

#### 4. 利润报表分析
- 平台总利润统计
- 分加盟商利润明细
- 批发价与实际成本对比

#### 5. 服务商配置
- API 服务商密钥管理
- 上游成本配置
- H3 账号池调度

### 认证机制

系统支持两种认证方式：

1. **OAuth2 Bearer Token (JWT)**
   - 适用于 Web 管理界面
   - 需要 super_admin 或 admin 角色
   - Token 有效期可配置

2. **API Key**
   - 适用于 Client 系统调用
   - 需要对应 scope 权限
   - 支持 TTL 过期策略

详见各端点文档中的 securitySchemes 说明。

### 版本控制

当前 API 版本：**v1**
所有路径前缀：`/api/v1/`

---

**维护者**: AI Team  
**最后更新**: 2026-09-20
""",
    version="1.0.0",
    contact={
        "name": "API Support",
        "email": "api-support@yourcompany.com",
    },
    openapi_url="/openapi.json",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add CORS middleware configuration
# This allows cross-origin requests from trusted domains only
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:5173",
        "https://admin.yourdomain.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, PUT, PATCH, DELETE, OPTIONS
    allow_headers=["*"],  # All headers including Authorization, Content-Type
    max_age=3600,  # Cache preflight results for 1 hour
)

# Register custom exception handlers for consistent error responses
HTTPExceptionHandlers.register(app)

# Add audit logging middleware
# This logs all write operations (POST/PUT/PATCH/DELETE) to audit table
app.add_middleware(AuditLoggerMiddleware)

# Mount API routers collection
# All v1 endpoints are registered in app/api/v1/routers/
app.include_router(v1_router, prefix="/api/v1")

# Mount Prometheus metrics endpoint
# /metrics path is exposed for monitoring dashboards (Grafana/Prometheus)
metrics_app = make_asgi_app("/metrics")
app.mount("/metrics", metrics_app)

__all__ = ["app"]
