from app.api.routes.domains import router as domains_router
from app.api.routes.mailboxes import router as mailboxes_router
from app.api.routes.tenants import router as tenants_router
from app.api.routes.wizard import router as wizard_router
from app.api.routes.stats import router as stats_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.step8 import router as step8_router
from app.api.routes.upload import router as upload_router
from app.api.routes.domain_removal import router as domain_removal_router
from app.api.routes.domain_lookup import router as domain_lookup_router
from app.api.routes.pipeline import router as pipeline_router
from app.api.routes.step8_endpoints import router as step8_endpoints_router
from app.api.routes.domain_checker import router as domain_checker_router
from app.api.routes.reconciliation import router as reconciliation_router
from app.api.routes.licensed_user_repair import router as licensed_user_repair_router

__all__ = [
    "domains_router",
    "mailboxes_router",
    "tenants_router",
    "wizard_router",
    "stats_router",
    "webhooks_router",
    "step8_router",
    "upload_router",
    "domain_removal_router",
    "domain_lookup_router",
    "pipeline_router",
    "step8_endpoints_router",
    "domain_checker_router",
    "reconciliation_router",
    "licensed_user_repair_router",
]
