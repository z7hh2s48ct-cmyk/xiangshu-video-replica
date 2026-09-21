"""API v1 router package."""

from fastapi import APIRouter

from app.api.v1.routers import auth, tenant, pricing, allowance

router = APIRouter()

# Include all routers
router.include_router(auth.router, tags=["Authentication"])
router.include_router(tenant.router, prefix="/tenants", tags=["Tenants"])
router.include_router(pricing.router, prefix="/pricing", tags=["Pricing"])
router.include_router(allowance.router, prefix="/allowance", tags=["Allowance Check"])
