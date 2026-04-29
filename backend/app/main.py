import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

# Create logs directory
os.makedirs("logs", exist_ok=True)

# Setup file logging
log_filename = f"logs/app_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(log_filename),
        logging.StreamHandler(sys.stdout),
    ],
)

# Silence SQLAlchemy SQL echo — floods logs and hides useful output.
# To temporarily re-enable for SQL debugging, set env var SQL_ECHO=1
if not os.getenv("SQL_ECHO"):
    logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.api.routes import (
    domains_router,
    mailboxes_router,
    tenants_router,
    wizard_router,
    stats_router,
    webhooks_router,
    step8_router,
    upload_router,
    domain_removal_router,
    domain_lookup_router,
    pipeline_router,
    step8_endpoints_router,
    domain_checker_router,
    reconciliation_router,
    licensed_user_repair_router,
)
from app.db.session import get_db_session
from app.core.config import get_settings
from app.db.session import engine
from app.services.powershell.setup import ensure_powershell_modules, check_powershell_available
from app.services.background_jobs import start_background_scheduler, stop_background_scheduler

logger = logging.getLogger(__name__)
logger.info("Logging to %s", log_filename)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting Cold Email Infrastructure API")
    # Startup: Test database connection
    logger.info("Testing database connection...")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        logger.info("Database connection successful")
    except Exception as e:
        logger.error("Database connection failed: %s", e)
        raise

    # Startup: Ensure PowerShell modules are installed (for M365 automation)
    logger.info("Checking PowerShell environment...")
    if check_powershell_available():
        if ensure_powershell_modules():
            logger.info("PowerShell environment ready - M365 automation enabled")
        else:
            logger.error("Failed to setup PowerShell modules - M365 automation will not work")
    else:
        logger.warning("PowerShell not available - M365 automation will not work")

    # Startup: Start background job scheduler (DKIM retry, etc.)
    logger.info("Starting background job scheduler...")
    start_background_scheduler()

    # Resume any pipelines that were running when container restarted
    from app.api.routes.pipeline import resume_interrupted_pipelines
    await resume_interrupted_pipelines()

    yield

    # Shutdown: Stop background scheduler
    logger.info("Stopping background job scheduler...")
    stop_background_scheduler()

    # Shutdown: Dispose engine
    await engine.dispose()
    logger.info("Database connection closed")


app = FastAPI(
    title="Cold Email Infrastructure API",
    version="1.0.0",
    description="API for managing cold email infrastructure: domains, tenants, and mailboxes",
    lifespan=lifespan,
    redirect_slashes=False,
)

# CORS configuration - allow frontend
allowed_origins = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
# Add any additional origins from settings
if settings.allowed_origins_list:
    allowed_origins.extend(settings.allowed_origins_list)

# In debug mode, allow all origins for easier development
if settings.debug:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


# Register routers
app.include_router(domains_router)
app.include_router(mailboxes_router)
app.include_router(tenants_router)
app.include_router(wizard_router)
app.include_router(stats_router)
app.include_router(webhooks_router)
app.include_router(step8_router)
app.include_router(upload_router)
app.include_router(domain_removal_router)
app.include_router(domain_lookup_router)
app.include_router(pipeline_router)
app.include_router(step8_endpoints_router)
app.include_router(domain_checker_router)
app.include_router(reconciliation_router)
app.include_router(licensed_user_repair_router)


@app.get("/", tags=["root"])
async def root() -> dict[str, Any]:
    """Root endpoint with API information."""
    return {
        "name": "Cold Email Infrastructure API",
        "version": "1.0.0",
        "description": "Manage domains, tenants, and mailboxes for cold email infrastructure",
        "endpoints": {
            "domains": "/api/v1/domains",
            "tenants": "/api/v1/tenants",
            "mailboxes": "/api/v1/mailboxes",
            "health": "/health",
            "docs": "/docs",
        },
    }


@app.get("/health", tags=["health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    logger.info("/health check requested")
    return {"status": "ok"}


@app.get("/health/db", tags=["health"])
async def health_check_db(db: AsyncSession = Depends(get_db_session)) -> dict:
    """Health check with database verification and data counts."""
    try:
        # Test raw connection
        result = await db.execute(text("SELECT 1"))
        db_connected = result.scalar() == 1
    except Exception as e:
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e)
        }
    
    # Count existing records
    try:
        from app.models.domain import Domain
        from app.models.tenant import Tenant
        from app.models.batch import SetupBatch
        
        batch_count = await db.scalar(select(func.count()).select_from(SetupBatch))
        domain_count = await db.scalar(select(func.count()).select_from(Domain))
        tenant_count = await db.scalar(select(func.count()).select_from(Tenant))
        
        return {
            "status": "healthy",
            "database": "connected",
            "data": {
                "batches": batch_count,
                "domains": domain_count,
                "tenants": tenant_count
            }
        }
    except Exception as e:
        return {
            "status": "degraded",
            "database": "connected",
            "error": f"Could not count records: {str(e)}"
        }
