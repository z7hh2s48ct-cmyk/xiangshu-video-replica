"""Database connection management for Superadmin API.

This module provides async database connection pooling using SQLAlchemy 2.0+
with psycopg async driver for PostgreSQL.

Key features:
- Async engine initialization with connection pool
- SessionLocal factory for scoped sessions
- Connection lifecycle management (startup/shutdown)
- Pool parameter optimization for high concurrency

Author: AI Implementation Team
Date: 2026-09-20
"""

import logging
from typing import Annotated

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config.settings import settings

logger = logging.getLogger(__name__)

# =====================================================
# Type Definitions
# =====================================================

# Annotated type for dependency injection
SessionDep = Annotated[AsyncSession, "Dependency in FastAPI"]

# =====================================================
# Engine Configuration
# =====================================================

def create_sync_engine() -> None:
    """Create and configure synchronous SQLAlchemy engine for Alembic migrations.
    
    This engine is used only for running migrations via Alembic, not for
    application data access (which uses async engine).
    
    Returns:
        SyncEngine instance configured for PostgreSQL
    
    Raises:
        RuntimeError: If database connection fails after retries
    """
    try:
        # Create sync engine (no connection pool needed for migrations)
        engine = create_engine(
            settings.database_url,
            echo=settings.debug,  # Log SQL queries in debug mode
            pool_pre_ping=True,   # Automatically verify connections before use
        )
        
        # Test connection immediately
        with engine.connect() as conn:
            logger.info("✓ Synchronous database connection successful")
            
    except (OperationalError, ProgrammingError) as e:
        error_msg = f"Failed to connect to database: {e}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


async def create_async_engine() -> None:
    """Create and configure asynchronous SQLAlchemy engine.
    
    This engine is used for all application data access. It supports:
    - Async I/O operations (non-blocking)
    - Connection pooling for high concurrency
    - Automatic transaction rollback on errors
    
    Returns:
        AsyncEngine instance
        
    Raises:
        RuntimeError: If database connection or pool setup fails
    """
    try:
        # Create async engine with optimized pool settings
        engine = create_async_engine(
            settings.database_url,
            echo=settings.debug,  # Log SQL queries in debug mode
            poolclass=None,       # Use default NullPool for dev, pool.SqlalchemyPool for prod
            pool_size=settings.pool_size,
            max_overflow=settings.max_overflow,
            pool_timeout=30,      # Seconds to wait for available connection
            pool_recycle=1800,    # Recycle connections after 30 minutes
            pool_pre_ping=True,   # Verify connection health before each use
            future=True,          # Use SQLAlchemy 2.0 style APIs
        )
        
        # Test async connection immediately
        async with engine.connect() as conn:
            logger.info("✓ Asynchronous database connection successful")
            
    except (OperationalError, ProgrammingError) as e:
        error_msg = f"Failed to initialize async database engine: {e}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


# =====================================================
# Session Factory
# =====================================================

# Create async session factory bound to the engine
_async_session_factory = async_sessionmaker(
    bind=None,  # Will be set by async engine later
    class_=AsyncSession,
    expire_on_commit=False,  # Don't auto-expire objects when commit ends
    autoflush=False,         # Manual control over when flushes occur
    autocommit=False,        # Explicit transaction boundaries required
)


def get_db_session() -> None:
    """Create and return a new database session.
    
    This function should NOT be called directly. Instead, use it as a
    FastAPI dependency in route handlers:
    
        @router.get("/tenants")
        async def list_tenants(db: SessionDep = Depends(get_db)):
            ...
    
    Returns:
        AsyncSession instance for the current request
        
    Usage pattern in routes:
        ```python
        from app.database import get_db
        from fastapi import Depends
        
        async def my_endpoint(db: SessionDep = Depends(get_db)):
            # Execute database operations
            results = await db.execute(select(Tenant))
        ```
    """
    if _async_session_factory.bind is None:
        raise RuntimeError("Database engine not initialized. Call init_db() first.")
    
    return _async_session_factory()


# =====================================================
# Lifecycle Management
# =====================================================

async def init_db():
    """Initialize database engine and session factory at startup.
    
    Called by FastAPI lifespan event handler during server start.
    Performs:
    1. Create async engine with connection pool
    2. Bind session factory to engine
    3. Run initial migration check
    """
    logger.info("🔄 Initializing database connection pool...")
    
    # Create async engine
    await create_async_engine()
    
    # Bind session factory to engine
    _async_session_factory.configure(bind=_async_session_factory.__dict__.get('_bind'))  # type: ignore
    
    # Run Alembic migrations automatically (if enabled)
    from alembic.command import upgrade
    from alembic.config import Config
    
    try:
        alembic_cfg = Config("alembic.ini")
        upgrade(alembic_cfg, "head")
        logger.info("✅ Database migrations completed successfully")
    except Exception as e:
        logger.warning(f"⚠️  Migration skipped: {e}")


async def close_db_connection():
    """Close all database connections gracefully at shutdown.
    
    Called by FastAPI lifespan event handler during server shutdown.
    Ensures all pooled connections are properly terminated.
    """
    logger.info("👋 Closing database connections...")
    
    try:
        # Dispose of engine (closes all connections)
        async_engine = _async_session_factory.kw.get('bind')
        if async_engine:
            await async_engine.dispose()
            logger.info("✓ Database engine disposed successfully")
    except Exception as e:
        logger.error(f"❌ Error closing database connections: {e}")


# =====================================================
# Dependency Injection Helper
# =====================================================

async def get_db_session_dep() -> AsyncSession:
    """FastAPI dependency that injects a database session.
    
    This function should be imported and used in all route handlers
    that need database access:
    
        from fastapi import APIRouter, Depends
        from app.database import get_db_session_dep
        
        router = APIRouter()
        
        @router.get("/tenants")
        async def list_tenants(db: AsyncSession = Depends(get_db_session_dep)):
            # db is automatically closed after response sent
            pass
    
    Yields:
        AsyncSession instance for the current request
        Session is automatically closed when generator exits
    """
    session = get_db_session()
    try:
        yield session
    finally:
        await session.close()


__all__ = [
    "init_db",
    "close_db_connection",
    "get_db_session_dep",
    "SessionDep",
]
