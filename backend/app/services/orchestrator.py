"""
Setup Orchestrator

Coordinates the complete tenant setup:
1. First login (Selenium)
2. OAuth tokens (Device code)
3. Domain setup (Graph API)
4. DNS records (Cloudflare)
5. DKIM (PowerShell)
6. Mailboxes (PowerShell)
"""

import asyncio
import logging
from typing import Optional, Callable
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant import Tenant
from app.models.domain import Domain
from app.models.mailbox import Mailbox
from app.services.selenium import MicrosoftLoginAutomation
from app.services.microsoft import DeviceCodeAuth, GraphClient
from app.services.powershell import ExchangeOperations
from app.services.cloudflare import cloudflare_service
from app.services.email_generator import generate_email_addresses

logger = logging.getLogger(__name__)


class SetupStep(str, Enum):
    FIRST_LOGIN = "first_login"
    OAUTH_TOKENS = "oauth_tokens"
    ADD_DOMAIN = "add_domain"
    VERIFY_DOMAIN = "verify_domain"
    DNS_RECORDS = "dns_records"
    DKIM_INIT = "dkim_init"
    DKIM_ENABLE = "dkim_enable"
    CREATE_MAILBOXES = "create_mailboxes"
    COMPLETE = "complete"


@dataclass
class SetupConfig:
    """Configuration for tenant setup."""
    new_password: str
    first_name: str
    last_name: str
    mailboxes_per_tenant: int = 50


@dataclass
class SetupResult:
    """Result of tenant setup."""
    success: bool
    step: SetupStep
    error: Optional[str] = None


class TenantSetupOrchestrator:
    """Orchestrates complete tenant setup."""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def setup_tenant(
        self,
        tenant: Tenant,
        domain: Domain,
        config: SetupConfig,
        on_step: Callable[[SetupStep], None] = None
    ) -> SetupResult:
        """
        Run complete setup for a tenant.
        """
        raise RuntimeError(
            "Legacy Graph/Cloudflare tenant orchestrator is disabled. "
            "Use the Admin Center setup wizard pipeline for domain DNS and DKIM setup."
        )
        def update_step(step: SetupStep):
            tenant.setup_step = step.value
            if on_step:
                on_step(step)
        
        try:
            # === STEP 1: FIRST LOGIN ===
            if not tenant.first_login_completed:
                update_step(SetupStep.FIRST_LOGIN)
                
                automation = MicrosoftLoginAutomation(headless=True)
                result = await automation.complete_first_login(
                    admin_email=tenant.admin_email,
                    initial_password=tenant.admin_password,
                    new_password=config.new_password,
                    existing_totp=tenant.totp_secret
                )
                
                if not result.success:
                    raise Exception(f"First login failed: {result.error}")
                
                tenant.admin_password = result.new_password or config.new_password
                tenant.totp_secret = result.totp_secret
                tenant.first_login_completed = True
                tenant.first_login_at = datetime.utcnow()
                tenant.security_defaults_disabled = result.security_defaults_disabled
                await self.db.commit()
            
            # === STEP 2: GET OAUTH TOKENS ===
            if not tenant.access_token:
                update_step(SetupStep.OAUTH_TOKENS)
                
                auth = DeviceCodeAuth()
                tokens = await auth.get_tokens(
                    tenant_id=tenant.microsoft_tenant_id or tenant.onmicrosoft_domain.replace(".onmicrosoft.com", ""),
                    admin_email=tenant.admin_email,
                    admin_password=tenant.admin_password,
                    totp_secret=tenant.totp_secret,
                    headless=True
                )
                
                if not tokens:
                    raise Exception("Failed to get OAuth tokens")
                
                tenant.access_token = tokens.access_token
                tenant.refresh_token = tokens.refresh_token
                tenant.token_expires_at = tokens.expires_at
                await self.db.commit()
            
            # Create API clients
            graph = GraphClient(tenant.access_token)
            
            # === STEP 3: ADD DOMAIN TO M365 ===
            if not tenant.domain_added_to_m365:
                update_step(SetupStep.ADD_DOMAIN)
                
                existing = await graph.get_domain(domain.name)
                if existing and existing.is_verified:
                    tenant.domain_added_to_m365 = True
                    tenant.domain_verified_in_m365 = True
                elif existing:
                    tenant.domain_added_to_m365 = True
                else:
                    await graph.add_domain(domain.name)
                    tenant.domain_added_to_m365 = True
                
                tenant.custom_domain = domain.name
                await self.db.commit()
            
            # === STEP 4: VERIFY DOMAIN ===
            if not tenant.domain_verified_in_m365:
                update_step(SetupStep.VERIFY_DOMAIN)
                
                # Get verification TXT
                records = await graph.get_verification_records(domain.name)
                txt_record = next((r for r in records if r.record_type == "TXT"), None)
                
                if txt_record and txt_record.text:
                    tenant.m365_verification_txt = txt_record.text
                    
                    # Add to Cloudflare - use idempotent method (won't fail if already exists)
                    await cloudflare_service.ensure_txt_record(
                        zone_id=domain.cloudflare_zone_id,
                        name="@",
                        content=txt_record.text,
                        domain=domain.name
                    )
                    logger.info(f"Verification TXT record ensured for {domain.name}")
                    
                    await asyncio.sleep(30)  # Wait for DNS
                    
                    # Verify
                    for _ in range(5):
                        if await graph.verify_domain(domain.name):
                            tenant.domain_verified_in_m365 = True
                            tenant.domain_verified_at = datetime.utcnow()
                            break
                        await asyncio.sleep(30)
                
                if not tenant.domain_verified_in_m365:
                    raise Exception("Domain verification failed")
                
                await self.db.commit()
            
            # === STEP 5: DNS RECORDS ===
            if not tenant.mx_record_added:
                update_step(SetupStep.DNS_RECORDS)
                
                zone_id = domain.cloudflare_zone_id
                
                # Use idempotent method - creates only missing records
                dns_results = await cloudflare_service.ensure_email_dns_records(zone_id, domain.name)
                
                if dns_results["mx"]["success"]:
                    tenant.mx_record_added = True
                    logger.info(f"MX record ensured for {domain.name}")
                else:
                    logger.warning(f"MX record failed for {domain.name}: {dns_results['mx'].get('error')}")
                
                if dns_results["spf"]["success"]:
                    tenant.spf_record_added = True
                    logger.info(f"SPF record ensured for {domain.name}")
                else:
                    logger.warning(f"SPF record failed for {domain.name}: {dns_results['spf'].get('error')}")
                
                if dns_results["autodiscover"]["success"]:
                    tenant.autodiscover_added = True
                    logger.info(f"Autodiscover record ensured for {domain.name}")
                else:
                    logger.warning(f"Autodiscover record failed for {domain.name}: {dns_results['autodiscover'].get('error')}")
                
                await self.db.commit()
            
            # === STEP 6: DKIM INIT ===
            exchange = ExchangeOperations(
                tenant.access_token,
                tenant.microsoft_tenant_id or tenant.onmicrosoft_domain
            )
            
            if not tenant.dkim_selector1:
                update_step(SetupStep.DKIM_INIT)
                
                dkim = await exchange.init_dkim(domain.name)
                tenant.dkim_selector1 = dkim.selector1_cname
                tenant.dkim_selector2 = dkim.selector2_cname
                
                # Use idempotent method - creates only missing CNAMEs (MUST be unproxied!)
                zone_id = domain.cloudflare_zone_id
                dkim_results = await cloudflare_service.ensure_dkim_cnames(
                    zone_id=zone_id,
                    domain=domain.name,
                    selector1_value=dkim.selector1_cname,
                    selector2_value=dkim.selector2_cname
                )
                
                if dkim_results["selector1_id"] and dkim_results["selector2_id"]:
                    tenant.dkim_cnames_added = True
                    logger.info(f"DKIM CNAMEs ensured for {domain.name}")
                else:
                    if dkim_results["errors"]:
                        logger.warning(f"DKIM CNAME errors for {domain.name}: {dkim_results['errors']}")
                
                await self.db.commit()
            
            # === STEP 7: ENABLE DKIM ===
            if not tenant.dkim_enabled:
                update_step(SetupStep.DKIM_ENABLE)
                
                await asyncio.sleep(60)  # Wait for CNAME propagation
                
                for _ in range(10):
                    if await exchange.enable_dkim(domain.name):
                        tenant.dkim_enabled = True
                        tenant.dkim_enabled_at = datetime.utcnow()
                        break
                    await asyncio.sleep(60)
                
                if not tenant.dkim_enabled:
                    raise Exception("DKIM enable failed")
                
                await self.db.commit()
            
            # === STEP 8: CREATE MAILBOXES ===
            if not tenant.mailboxes_created:
                update_step(SetupStep.CREATE_MAILBOXES)
                
                # Generate emails
                emails = generate_email_addresses(
                    config.first_name,
                    config.last_name,
                    domain.name,
                    config.mailboxes_per_tenant
                )
                
                # Save to DB
                for email_data in emails:
                    mailbox = Mailbox(
                        tenant_id=tenant.id,
                        batch_id=tenant.batch_id,
                        email=email_data["email"],
                        display_name=email_data["display_name"],
                        password=email_data["password"],
                        status="creating"
                    )
                    self.db.add(mailbox)
                
                await self.db.commit()
                tenant.mailboxes_generated = True
                
                # Create in Exchange
                delegate = tenant.admin_email
                created = 0
                
                for email_data in emails:
                    try:
                        if await exchange.create_and_delegate_mailbox(
                            email_data["email"],
                            email_data["display_name"],
                            delegate
                        ):
                            created += 1
                    except Exception as e:
                        logger.warning(f"Mailbox creation failed: {e}")
                
                tenant.mailbox_count = created
                tenant.mailboxes_created = True
                tenant.mailboxes_created_at = datetime.utcnow()
                tenant.delegation_completed = True
                
                await self.db.commit()
            
            # === COMPLETE ===
            tenant.status = "ready"
            await self.db.commit()
            
            return SetupResult(success=True, step=SetupStep.COMPLETE)
            
        except Exception as e:
            logger.exception(f"Setup failed: {e}")
            tenant.setup_error = str(e)
            tenant.status = "error"
            await self.db.commit()
            return SetupResult(
                success=False,
                step=SetupStep(tenant.setup_step) if tenant.setup_step else SetupStep.FIRST_LOGIN,
                error=str(e)
            )


async def process_batch(
    db: AsyncSession,
    batch_id: str,
    config: SetupConfig,
    max_workers: int = 10,
    chunk_size: int = 5,
    on_progress: Callable[[int, int], None] = None
):
    """Process all tenants in a batch."""
    from sqlalchemy import select
    from app.models.tenant import Tenant
    from app.models.domain import Domain
    
    # Get tenants
    result = await db.execute(
        select(Tenant).where(Tenant.batch_id == batch_id, Tenant.status != "ready")
    )
    tenants = result.scalars().all()
    
    # Get domains
    domains_by_id = {}
    for tenant in tenants:
        if tenant.domain_id:
            domain = await db.get(Domain, tenant.domain_id)
            if domain:
                domains_by_id[tenant.domain_id] = domain
    
    completed = 0
    semaphore = asyncio.Semaphore(max_workers)
    
    async def process_one(tenant: Tenant):
        nonlocal completed
        async with semaphore:
            domain = domains_by_id.get(tenant.domain_id)
            if domain:
                orchestrator = TenantSetupOrchestrator(db)
                await orchestrator.setup_tenant(tenant, domain, config)
            
            completed += 1
            if on_progress:
                on_progress(completed, len(tenants))
    
    # Process tenants in chunks to avoid scheduling everything at once
    for i in range(0, len(tenants), chunk_size):
        chunk = tenants[i:i + chunk_size]
        logger.info(
            "Processing tenant chunk %s (%s-%s of %s)",
            (i // chunk_size) + 1,
            i + 1,
            min(i + chunk_size, len(tenants)),
            len(tenants),
        )
        await asyncio.gather(*[process_one(t) for t in chunk], return_exceptions=True)
        await asyncio.sleep(2)
    
    return {"total": len(tenants), "completed": completed}
