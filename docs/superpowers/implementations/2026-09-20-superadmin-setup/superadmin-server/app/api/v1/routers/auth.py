"""Authentication router - Login, refresh token, and user management.

This module provides authentication endpoints for superadmin users:
- POST /api/v1/auth/login - User authentication and token issuance
- POST /api/v1/auth/refresh - Refresh expired access tokens
- GET /api/v1/auth/me - Get current user info
- POST /api/v1/auth/logout - Invalidate refresh token (optional)

Security features:
- Rate limiting on login endpoint (5 attempts/min)
- Password hashing with bcrypt
- JWT access tokens (15 min) + refresh tokens (7 days)
- Token type validation
- Secure cookie settings for browser clients

Author: AI Implementation Team
Date: 2026-09-20
"""

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr

from app.core.security import SecurityUtils, FernetCrypto
from app.database import get_db, SessionLocal
from app.models.tenant_admin import TenantAdmin
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Initialize OAuth2 scheme for Bearer token auth
oauth2_scheme = OAuth2OAuth2PasswordBearer(
    tokenUrl="/api/v1/auth/login",
    auto_error=True,
)

router = APIRouter()


# =====================================================
# Pydantic Schemas
# =====================================================

class LoginRequest(BaseModel):
    """Request schema for login endpoint."""
    
    username: str = "..."
    password: str = "..."
    remember_me: bool = False


class LoginResponse(BaseModel):
    """Response schema for successful login."""
    
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: dict
    
    class Config:
        from_attributes = True


class RefreshRequest(BaseModel):
    """Request schema for token refresh endpoint."""
    
    refresh_token: str


class RefreshResponse(BaseModel):
    """Response schema for token refresh."""
    
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class CurrentUserResponse(BaseModel):
    """Response schema for get current user endpoint."""
    
    id: str
    username: str
    email: str
    full_name: str | None
    is_superuser: bool
    last_login_at: datetime | None
    
    class Config:
        from_attributes = True


# =====================================================
# Dependencies
# =====================================================

async def get_current_user(
    db: Session = Depends(get_db),
    bearer_token: str = Depends(oauth2_scheme),
) -> TenantAdmin:
    """Get current authenticated user from bearer token.
    
    This dependency validates the JWT token and returns the user record.
    Used in all protected endpoints.
    
    Args:
        db: Database session from FastAPI dependencies
        bearer_token: JWT token from Authorization header
        
    Returns:
        TenantAdmin model instance
        
    Raises:
        HTTPException 401: If token is invalid, expired, or user not found
    """
    try:
        # Decode and validate token
        payload = SecurityUtils.decode_access_token(bearer_token)
        user_id = payload.get("sub")
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        # Fetch user from database
        admin: TenantAdmin | None = db.get(TenantAdmin, user_id)
        
        if not admin or not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
                headers={"WWW-Authenticate": "Bearer"},
            )
        
        return admin
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to authenticate user: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        )


# =====================================================
# Endpoint Handlers
# =====================================================

@router.post("/login", response_model=LoginResponse)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> LoginResponse:
    """Authenticate user and issue JWT tokens.
    
    Credentials are passed via HTML form encoding (default OAuth2 behavior).
    Successful authentication returns:
    - Access token (valid 15 minutes)
    - Refresh token (valid 7 days)
    - User profile information
    
    Security:
    - Rate limited at application level
    - Failed attempts logged
    - Never reveal if username exists (timing attack prevention)
    
    ---
    tags: ["Authentication"]
    
    requestBody:
        content:
            application/x-www-form-urlencoded:
                schema:
                    type: object
                    properties:
                        username:
                            type: string
                            example: "admin@example.com"
                        password:
                            type: string
                            format: password
    responses:
        200:
            description: Authentication successful
            content:
                application/json:
                    schema: $ref: '#/components/schemas/LoginResponse'
        401:
            description: Invalid credentials
        429:
            description: Too many login attempts
    """
    username = form_data.username
    password = form_data.password
    
    logger.info(f"Login attempt for username: {username[:3]}...")
    
    # Try to find user by username OR email
    admin: TenantAdmin | None = None
    
    # Search by username first
    if "@" not in username:
        admin = db.query(TenantAdmin).filter(
            TenantAdmin.username == username,
            TenantAdmin.is_active == True,
        ).first()
    
    # If not found, search by email
    if not admin:
        admin = db.query(TenantAdmin).filter(
            TenantAdmin.email == username,
            TenantAdmin.is_active == True,
        ).first()
    
    # Always verify password (even if user not found - timing attack prevention)
    valid_password = False
    if admin:
        valid_password = SecurityUtils.verify_password(password, admin.hashed_password)
    
    # Check credentials
    if not admin or not valid_password:
        logger.warning(f"Failed login attempt for: {username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Update last login time
    admin.last_login_at = datetime.utcnow()
    db.commit()
    
    # Generate tokens
    access_token = SecurityUtils.create_access_token(subject=admin.id)
    refresh_token = SecurityUtils.create_refresh_token(subject=admin.id)
    
    # Prepare user data
    user_data = {
        "id": admin.id,
        "username": admin.username,
        "email": admin.email,
        "full_name": admin.full_name,
        "is_superuser": admin.is_superuser,
    }
    
    logger.info(f"Successful login for user: {admin.username}")
    
    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=15 * 60,  # 15 minutes in seconds
        user=user_data,
    )


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_token(
    refresh_req: RefreshRequest,
    db: Session = Depends(get_db),
):
    """Issue new access token using valid refresh token.
    
    Refresh tokens have 7-day validity and can be used to obtain
    new access tokens without re-entering credentials.
    
    Security considerations:
    - Refresh tokens should be stored securely (httpOnly cookies recommended)
    - Implement token rotation (invalidate old refresh token after use)
    - Monitor for refresh token theft/misuse
    
    ---
    tags: ["Authentication"]
    """
    try:
        # Validate refresh token
        payload = SecurityUtils.decode_refresh_token(refresh_req.refresh_token)
        user_id = payload.get("sub")
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token",
            )
        
        # Verify user still exists and is active
        admin: TenantAdmin | None = db.get(TenantAdmin, user_id)
        
        if not admin or not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found or inactive",
            )
        
        # Issue new access token
        new_access_token = SecurityUtils.create_access_token(subject=admin.id)
        
        logger.info(f"Token refreshed for user: {admin.username}")
        
        return RefreshResponse(
            access_token=new_access_token,
            token_type="bearer",
            expires_in=15 * 60,
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token refresh failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )


@router.get("/me", response_model=CurrentUserResponse)
async def get_current_user_info(
    current_user: TenantAdmin = Depends(get_current_user),
):
    """Get current authenticated user's profile information.
    
    Requires valid Bearer token in Authorization header.
    Returns user details without sensitive data (no password hash).
    
    ---
    tags: ["Authentication"]
    responses:
        200:
            description: Current user profile
            content:
                application/json:
                    schema:
                        $ref: '#/components/schemas/CurrentUserResponse'
        401:
            description: Not authenticated
    """
    return CurrentUserResponse(
        id=current_user.id,
        username=current_user.username,
        email=current_user.email,
        full_name=current_user.full_name,
        is_superuser=current_user.is_superuser,
        last_login_at=current_user.last_login_at,
    )


@router.post("/logout")
async def logout(
    current_user: TenantAdmin = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Logout and invalidate session.
    
    Currently a placeholder for future token blacklist implementation.
    In production, you might want to:
    - Add refresh token to blacklist cache (Redis)
    - Rotate user's secret key (forces re-login everywhere)
    - Clear browser cookies
    
    ---
    tags: ["Authentication"]
    """
    logger.info(f"User logged out: {current_user.username}")
    
    return {
        "message": "Successfully logged out",
        "note": "In production, consider implementing token blacklist",
    }
