"""
Domain Removal Service - Orchestrates complete domain removal from tenants.

Supports two modes:
  Mode 1 (DB): Given domain names, looks up tenant/mailboxes/Cloudflare from database
  Mode 2 (CSV): Given domain + tenant credentials directly, no DB lookup needed

For both modes, the removal steps are:
1. Remove domain from M365 (Graph API forceDelete primary, Selenium fallback)
   - Graph API forceDelete automatically reassigns all UPNs, mailboxes, proxy addresses, etc.
2. Clean up Cloudflare DNS records
3. Update database (Mode 1 only, or Mode 2 if domain happens to exist in DB)
"""
import asyncio
import csv
import io
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.domain import Domain, DomainStatus
from app.models.tenant import Tenant, TenantStatus
from app.models.mailbox import Mailbox, MailboxStatus
from app.services.cloudflare import CloudflareService
from app.services.powershell.runner import PowerShellRunner

logger = logging.getLogger(__name__)


class DomainRemovalService:
    """Orchestrates domain removal from M365 tenants."""
    
    def __init__(self):
        self.ps_runner = PowerShellRunner()
    
    def _get_cf_service(self) -> Optional[CloudflareService]:
        """Get a CloudflareService instance, or None if not configured."""
        try:
            return CloudflareService()
        except Exception as e:
            logger.warning(f"CloudflareService not available: {e}")
            return None
    
    # =========================================================
    # CSV PARSING
    # =========================================================
    
    def parse_removal_csv(self, csv_content: str) -> List[Dict[str, str]]:
        """
        Parse CSV with domain + tenant credentials.
        
        Expected columns: domain, admin_email, admin_password
        Optional columns: totp_secret, cloudflare_zone_id
        """
        reader = csv.DictReader(io.StringIO(csv_content))
        
        entries = []
        for row_num, row in enumerate(reader, start=2):
            # Normalize column names (strip whitespace, lowercase, replace spaces with underscores)
            normalized = {
                k.strip().lower().replace(" ", "_"): v.strip() if v else ""
                for k, v in row.items()
            }
            
            domain = normalized.get("domain", "").lower()
            admin_email = normalized.get("admin_email", "")
            admin_password = normalized.get("admin_password", "")
            totp_secret = normalized.get("totp_secret", "") or None
            cloudflare_zone_id = normalized.get("cloudflare_zone_id", "") or None
            
            if not domain or not admin_email or not admin_password:
                logger.warning(f"Row {row_num}: Missing required fields (domain={domain}, admin_email={admin_email}) - skipping")
                continue
            
            entries.append({
                "domain": domain,
                "admin_email": admin_email,
                "admin_password": admin_password,
                "totp_secret": totp_secret,
                "cloudflare_zone_id": cloudflare_zone_id,
                "source": "csv",
                "row": row_num
            })
        
        return entries
    
    # =========================================================
    # VALIDATION
    # =========================================================
    
    async def validate_domains_from_db(self, db: AsyncSession, domain_names: List[str]) -> Dict[str, Any]:
        """Pre-flight check for Mode 1 (DB domains). Returns validation results."""
        results = []
        
        for domain_name in domain_names:
            domain_name = domain_name.strip().lower()
            
            # Look up domain with tenant relationship
            result = await db.execute(
                select(Domain).options(selectinload(Domain.tenant)).where(Domain.name == domain_name)
            )
            domain = result.scalar_one_or_none()
            
            if not domain:
                results.append({
                    "domain": domain_name,
                    "can_remove": False,
                    "reason": "Domain not found in database",
                    "source": "db",
                    "tenant": None,
                    "mailbox_count": 0
                })
                continue
            
            # Try to find the tenant - check both sides of the relationship
            tenant = domain.tenant
            
            if not domain.tenant_id or not tenant:
                # Fallback: check if a tenant links to this domain via Tenant.domain_id
                # (auto_link_domains previously only set tenant.domain_id, not domain.tenant_id)
                reverse_result = await db.execute(
                    select(Tenant).where(Tenant.domain_id == domain.id)
                )
                tenant = reverse_result.scalar_one_or_none()
                
                if tenant:
                    # Fix the missing reverse link for future lookups
                    domain.tenant_id = tenant.id
                    await db.commit()
                    logger.info(f"[{domain_name}] Fixed missing domain.tenant_id -> {tenant.id} (was only linked via tenant.domain_id)")
                else:
                    results.append({
                        "domain": domain_name,
                        "can_remove": False,
                        "reason": "Domain not linked to any tenant",
                        "source": "db",
                        "tenant": None,
                        "mailbox_count": 0,
                        "domain_id": str(domain.id)
                    })
                    continue
            
            # Count mailboxes for this domain
            mailbox_result = await db.execute(
                select(Mailbox).where(Mailbox.tenant_id == tenant.id)
            )
            mailboxes = mailbox_result.scalars().all()
            domain_mailboxes = [m for m in mailboxes if m.email and m.email.endswith(f"@{domain_name}")]
            
            results.append({
                "domain": domain_name,
                "can_remove": True,
                "reason": "Ready for removal",
                "source": "db",
                "domain_id": str(domain.id),
                "tenant": {
                    "id": str(tenant.id),
                    "name": tenant.name,
                    "onmicrosoft_domain": tenant.onmicrosoft_domain,
                    "admin_email": tenant.admin_email
                } if tenant else None,
                "mailbox_count": len(domain_mailboxes),
                "cloudflare_zone_id": domain.cloudflare_zone_id,
                "has_dkim": domain.dkim_enabled,
            })
        
        removable = [r for r in results if r["can_remove"]]
        return {
            "total": len(results),
            "removable": len(removable),
            "not_removable": len(results) - len(removable),
            "domains": results
        }
    
    def validate_csv_entries(self, entries: List[Dict[str, str]]) -> Dict[str, Any]:
        """Pre-flight check for Mode 2 (CSV entries)."""
        results = []
        for entry in entries:
            issues = []
            if not entry["domain"] or "." not in entry["domain"]:
                issues.append("Invalid domain name")
            if not entry["admin_email"] or "@" not in entry["admin_email"]:
                issues.append("Invalid admin email")
            if not entry.get("admin_password"):
                issues.append("Missing admin password")
            
            onmicrosoft = entry["admin_email"].split("@")[1] if "@" in entry["admin_email"] else "unknown"
            
            results.append({
                "domain": entry["domain"],
                "can_remove": len(issues) == 0,
                "reason": "; ".join(issues) if issues else "Ready for removal",
                "source": "csv",
                "row": entry.get("row"),
                "tenant": {
                    "admin_email": entry["admin_email"],
                    "onmicrosoft_domain": onmicrosoft
                },
                "has_totp": bool(entry.get("totp_secret")),
                "mailbox_count": "Unknown (will discover via Exchange)"
            })
        
        removable = [r for r in results if r["can_remove"]]
        return {
            "total": len(results),
            "removable": len(removable),
            "not_removable": len(results) - len(removable),
            "domains": results
        }
    
    # =========================================================
    # CLOUDFLARE ZONE LOOKUP
    # =========================================================
    
    async def find_cloudflare_zone_id(self, domain_name: str) -> Optional[str]:
        """Try to find Cloudflare zone ID by domain name via API."""
        cf_service = self._get_cf_service()
        if not cf_service:
            return None
        try:
            zone_info = await cf_service.get_zone_by_name(domain_name)
            if zone_info:
                return zone_info["zone_id"]
        except Exception as e:
            logger.warning(f"Could not search Cloudflare zones for {domain_name}: {e}")
        return None
    
    # =========================================================
    # SHARED REMOVAL LOGIC (used by both modes)
    # =========================================================
    
    async def _execute_removal(
        self,
        domain_name: str,
        admin_email: str,
        admin_password: str,
        totp_secret: Optional[str],
        cloudflare_zone_id: Optional[str],
        skip_m365: bool,
        headless: bool,
        max_retries: int = 2,
        licensed_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Core removal logic shared by both Mode 1 and Mode 2.
        
        Uses the robust two-pronged approach:
        1. Graph API forceDelete (primary) — handles ALL reassignment server-side
        2. Selenium Admin Portal (fallback) — if Graph API fails
        3. Clean up Cloudflare DNS records
        
        Graph API forceDelete automatically reassigns all UPNs, mailboxes,
        proxy addresses, groups, etc. — no need for separate PowerShell steps.
        
        Returns dict with "steps" key containing results of each step.
        """
        steps = {}
        
        # ===== STEP 1: Remove domain from M365 (Graph API primary, Selenium fallback) =====
        if not skip_m365:
            from app.services.selenium.domain_removal import remove_domain_robust
            
            total_attempts = 1 + max_retries  # 1 initial + N retries
            m365_result = None
            
            for attempt in range(total_attempts):
                try:
                    if attempt == 0:
                        logger.info(f"[{domain_name}] Step 1: Removing domain from M365 (Graph API + Selenium fallback)...")
                    else:
                        retry_delay = 30 * attempt  # 30s, 60s, etc.
                        logger.info(f"[{domain_name}] Step 1: RETRY {attempt}/{max_retries} — waiting {retry_delay}s before retrying...")
                        await asyncio.sleep(retry_delay)
                    
                    # Run synchronous robust removal in thread pool
                    m365_result = await asyncio.to_thread(
                        remove_domain_robust,
                        domain_name=domain_name,
                        admin_email=admin_email,
                        admin_password=admin_password,
                        totp_secret=totp_secret,
                        headless=headless,
                        licensed_user_id=licensed_user_id,
                    )
                    
                    if m365_result.get("success"):
                        logger.info(f"[{domain_name}] M365 removal succeeded on attempt {attempt + 1} via {m365_result.get('method', 'unknown')}")
                        m365_result["attempts"] = attempt + 1
                        break
                    else:
                        needs_retry = m365_result.get("needs_retry", False)
                        if needs_retry and attempt < total_attempts - 1:
                            logger.warning(f"[{domain_name}] M365 removal failed on attempt {attempt + 1}: {m365_result.get('error')} — will retry")
                            continue
                        elif not needs_retry:
                            logger.error(f"[{domain_name}] M365 removal failed (non-retryable): {m365_result.get('error')}")
                            m365_result["attempts"] = attempt + 1
                            break
                        else:
                            logger.error(f"[{domain_name}] M365 removal failed after all {attempt + 1} attempts: {m365_result.get('error')}")
                            m365_result["attempts"] = attempt + 1
                            break
                except Exception as e:
                    logger.error(f"[{domain_name}] Error removing from M365 (attempt {attempt + 1}): {e}")
                    m365_result = {"success": False, "error": str(e), "attempts": attempt + 1}
                    if attempt < total_attempts - 1:
                        continue
                    break
            
            steps["m365_removal"] = m365_result or {"success": False, "error": "No result from M365 removal"}
            steps["license_cleanup"] = steps["m365_removal"].get("license_cleanup", {})
            if not steps["m365_removal"].get("success"):
                steps["cloudflare_cleanup"] = {"skipped": True, "note": "M365 removal failed; DNS preserved for retry"}
                return {"steps": steps}
        else:
            steps["m365_removal"] = {"skipped": True, "note": "M365 operations skipped"}
            steps["license_cleanup"] = {"skipped": True, "note": "M365 operations skipped"}
        
        # ===== STEP 2: Clean up Cloudflare DNS records =====
        try:
            logger.info(f"[{domain_name}] Step 2: Cleaning Cloudflare DNS...")
            
            cf_service = self._get_cf_service()
            
            zone_id = cloudflare_zone_id
            if not zone_id:
                zone_id = await self.find_cloudflare_zone_id(domain_name)
            
            if zone_id and cf_service:
                all_records = await cf_service.list_dns_records(zone_id)
                
                # Identify M365-specific records to delete
                records_to_delete = []
                for record in all_records:
                    rname = record.get("name", "").lower()
                    rcontent = record.get("content", "").lower()
                    rtype = record.get("type", "")
                    
                    should_delete = False
                    reason = ""
                    
                    # M365 MX record
                    if rtype == "MX" and "mail.protection.outlook.com" in rcontent:
                        should_delete, reason = True, "M365 MX"
                    # M365 SPF record
                    elif rtype == "TXT" and "spf.protection.outlook.com" in rcontent:
                        should_delete, reason = True, "M365 SPF"
                    # M365 verification TXT (MS=msXXXXXXXX)
                    elif rtype == "TXT" and rcontent.startswith("ms="):
                        should_delete, reason = True, "M365 verification TXT"
                    # DKIM CNAME records
                    elif rtype == "CNAME" and "_domainkey" in rname:
                        should_delete, reason = True, "DKIM CNAME"
                    # Autodiscover CNAME
                    elif rtype == "CNAME" and "autodiscover" in rname:
                        should_delete, reason = True, "Autodiscover CNAME"
                    
                    if should_delete:
                        records_to_delete.append({
                            "id": record["id"],
                            "type": rtype,
                            "name": rname,
                            "reason": reason
                        })
                
                deleted, failed = [], []
                for rec in records_to_delete:
                    try:
                        await cf_service.delete_dns_record(zone_id, rec["id"])
                        deleted.append(rec)
                    except Exception as e:
                        rec["error"] = str(e)
                        failed.append(rec)
                
                steps["cloudflare_cleanup"] = {
                    "success": len(failed) == 0,
                    "records_deleted": len(deleted),
                    "records_failed": len(failed),
                    "deleted": deleted,
                    "failed": failed if failed else None,
                    "zone_kept": True  # We keep the zone, only remove M365 records
                }
            else:
                steps["cloudflare_cleanup"] = {
                    "success": True,
                    "note": "No Cloudflare zone found or Cloudflare not configured - skipping"
                }
        except Exception as e:
            logger.error(f"[{domain_name}] Error cleaning Cloudflare: {e}")
            steps["cloudflare_cleanup"] = {"success": False, "error": str(e)}
        
        return {"steps": steps}
    
    # =========================================================
    # MODE 1: Remove using database records
    # =========================================================
    
    async def remove_domain_from_db(
        self,
        db: AsyncSession,
        domain_name: str,
        skip_m365: bool = False,
        headless: bool = True,
        max_retries: int = 2
    ) -> Dict[str, Any]:
        """
        Mode 1: Remove a domain using database records.
        
        Looks up the domain, its tenant credentials, and Cloudflare zone from the DB,
        then executes the full removal flow.
        """
        domain_name = domain_name.strip().lower()
        result = {
            "domain": domain_name,
            "source": "db",
            "success": False,
            "steps": {},
            "error": None,
            "started_at": datetime.utcnow().isoformat()
        }
        
        # Look up domain and tenant from database
        domain_result = await db.execute(
            select(Domain).options(selectinload(Domain.tenant)).where(Domain.name == domain_name)
        )
        domain = domain_result.scalar_one_or_none()
        
        if not domain:
            result["error"] = f"Domain '{domain_name}' not found in database"
            return result
        
        # Try to find the tenant - check both sides of the relationship
        tenant = domain.tenant
        
        if not domain.tenant_id or not tenant:
            # Fallback: check if a tenant links to this domain via Tenant.domain_id
            # (auto_link_domains previously only set tenant.domain_id, not domain.tenant_id)
            reverse_result = await db.execute(
                select(Tenant).where(Tenant.domain_id == domain.id)
            )
            tenant = reverse_result.scalar_one_or_none()
            
            if tenant:
                # Fix the missing reverse link
                domain.tenant_id = tenant.id
                await db.commit()
                logger.info(f"[{domain_name}] Fixed missing domain.tenant_id -> {tenant.id} during removal")
            else:
                result["error"] = f"Domain '{domain_name}' is not linked to any tenant"
                return result
        result["tenant_name"] = tenant.name
        
        # Execute the shared removal logic
        removal = await self._execute_removal(
            domain_name=domain_name,
            admin_email=tenant.admin_email,
            admin_password=tenant.admin_password,
            totp_secret=getattr(tenant, 'totp_secret', None),
            cloudflare_zone_id=domain.cloudflare_zone_id,
            skip_m365=skip_m365,
            headless=headless,
            max_retries=max_retries,
            licensed_user_id=domain.licensed_user_id,
        )
        result["steps"] = removal["steps"]
        
        # ===== CHECK: Did M365 removal succeed? =====
        # CRITICAL: Only clean up the database if M365 removal was confirmed successful
        # or was intentionally skipped. If M365 removal failed, we MUST keep the tenant
        # link intact so the system can retry the removal later.
        m365_ok = (
            result["steps"].get("m365_removal", {}).get("success", False)
            or result["steps"].get("m365_removal", {}).get("skipped", False)
        )
        
        if not m365_ok:
            # M365 removal FAILED — do NOT clear the database
            # Mark domain as PROBLEM so it's visible, but keep tenant link for retry
            m365_error = result["steps"].get("m365_removal", {}).get("error", "Unknown error")
            m365_attempts = result["steps"].get("m365_removal", {}).get("attempts", 1)
            
            logger.error(
                f"[{domain_name}] M365 removal FAILED after {m365_attempts} attempt(s): {m365_error} "
                f"— keeping tenant link intact for retry (tenant: {tenant.name})"
            )
            
            try:
                domain.status = DomainStatus.PROBLEM
                domain.error_message = (
                    f"M365 removal failed after {m365_attempts} attempt(s): {m365_error} "
                    f"(tenant: {tenant.name}, {datetime.utcnow().isoformat()})"
                )
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"[{domain_name}] Could not update domain status to PROBLEM: {e}")
            
            result["success"] = False
            result["error"] = f"M365 removal failed: {m365_error}"
            result["steps"]["database_update"] = {
                "success": True,
                "note": "Domain marked as PROBLEM — tenant link preserved for retry",
                "m365_failed": True
            }
            result["completed_at"] = datetime.utcnow().isoformat()
            return result
        
        # ===== STEP 3: Update database (only if M365 removal succeeded) =====
        try:
            logger.info(f"[{domain_name}] Step 3: Updating database (M365 removal confirmed)...")
            
            # Archive/retire mailboxes for this domain
            mb_result = await db.execute(
                select(Mailbox).where(Mailbox.tenant_id == tenant.id)
            )
            mailboxes = [
                m for m in mb_result.scalars().all()
                if m.email and m.email.endswith(f"@{domain_name}")
            ]
            for mb in mailboxes:
                mb.status = MailboxStatus.SUSPENDED
                mb.error_message = f"Domain removed from tenant at {datetime.utcnow().isoformat()}"
            
            # Unlink domain from tenant
            domain.tenant_id = None
            if hasattr(tenant, 'domain_id') and tenant.domain_id == domain.id:
                tenant.domain_id = None
                if hasattr(tenant, 'custom_domain'):
                    tenant.custom_domain = None
            
            # Reset domain status and flags back to "purchased" state
            domain.status = DomainStatus.PURCHASED
            domain.dns_records_created = False
            domain.mx_configured = False
            domain.spf_configured = False
            domain.dmarc_configured = False
            domain.dkim_cnames_added = False
            domain.dkim_enabled = False
            domain.dkim_selector1_cname = None
            domain.dkim_selector2_cname = None
            domain.verification_txt_value = None
            domain.verification_txt_added = False
            self._reset_license_state(domain, skip_m365)
            domain.batch_id = None
            domain.error_message = f"Removed from tenant '{tenant.name}' at {datetime.utcnow().isoformat()}"
            
            # Reset M365 domain flags if they exist on the model
            if hasattr(domain, 'm365_domain_added'):
                domain.m365_domain_added = False
            if hasattr(domain, 'm365_domain_verified'):
                domain.m365_domain_verified = False
            
            await db.commit()
            result["steps"]["database_update"] = {
                "success": True,
                "mailboxes_archived": len(mailboxes)
            }
        except Exception as e:
            await db.rollback()
            logger.error(f"[{domain_name}] Database update failed: {e}")
            result["steps"]["database_update"] = {"success": False, "error": str(e)}
            result["error"] = f"Database update failed: {e}"
            return result
        
        # Determine overall success
        result["success"] = True
        result["completed_at"] = datetime.utcnow().isoformat()
        
        return result
    
    # =========================================================
    # MODE 2: Remove using CSV credentials
    # =========================================================
    
    async def remove_domain_from_csv(
        self,
        entry: Dict[str, str],
        db: AsyncSession = None,
        skip_m365: bool = False,
        headless: bool = True
    ) -> Dict[str, Any]:
        """
        Mode 2: Remove a domain using credentials from CSV.
        
        Uses the provided admin credentials directly. If the domain also exists
        in the database, it will clean up those records too.
        """
        domain_name = entry["domain"].strip().lower()
        result = {
            "domain": domain_name,
            "source": "csv",
            "csv_row": entry.get("row"),
            "success": False,
            "steps": {},
            "error": None,
            "started_at": datetime.utcnow().isoformat()
        }
        
        cloudflare_zone_id = entry.get("cloudflare_zone_id")
        
        # Check if domain also exists in DB (use its zone_id if available)
        db_domain = None
        if db:
            try:
                db_result = await db.execute(
                    select(Domain).where(Domain.name == domain_name)
                )
                db_domain = db_result.scalar_one_or_none()
                if db_domain and not cloudflare_zone_id:
                    cloudflare_zone_id = db_domain.cloudflare_zone_id
            except Exception as e:
                logger.warning(f"[{domain_name}] Could not check DB for domain: {e}")
        
        # If still no zone_id, try Cloudflare API lookup by name
        if not cloudflare_zone_id:
            cloudflare_zone_id = await self.find_cloudflare_zone_id(domain_name)
        
        # Execute the shared removal logic
        removal = await self._execute_removal(
            domain_name=domain_name,
            admin_email=entry["admin_email"],
            admin_password=entry["admin_password"],
            totp_secret=entry.get("totp_secret"),
            cloudflare_zone_id=cloudflare_zone_id,
            skip_m365=skip_m365,
            headless=headless,
            licensed_user_id=db_domain.licensed_user_id if db_domain else None,
        )
        result["steps"] = removal["steps"]
        
        # Determine success based on M365 removal
        m365_ok = (
            result["steps"].get("m365_removal", {}).get("success", False)
            or result["steps"].get("m365_removal", {}).get("skipped", False)
        )
        
        # CRITICAL: Only clean up DB records if M365 removal succeeded or was skipped.
        # If M365 removal failed, keep DB records intact for retry.
        if db_domain and db:
            if m365_ok:
                try:
                    db_domain.tenant_id = None
                    db_domain.status = DomainStatus.PURCHASED
                    db_domain.dns_records_created = False
                    db_domain.mx_configured = False
                    db_domain.spf_configured = False
                    db_domain.dkim_cnames_added = False
                    db_domain.dkim_enabled = False
                    db_domain.dkim_selector1_cname = None
                    db_domain.dkim_selector2_cname = None
                    db_domain.verification_txt_value = None
                    db_domain.verification_txt_added = False
                    self._reset_license_state(db_domain, skip_m365)
                    db_domain.batch_id = None
                    db_domain.error_message = f"Removed via CSV at {datetime.utcnow().isoformat()}"
                    await db.commit()
                    result["steps"]["database_update"] = {
                        "success": True,
                        "note": "Domain found in DB and cleaned up (M365 removal confirmed)"
                    }
                except Exception as e:
                    await db.rollback()
                    result["steps"]["database_update"] = {"success": False, "error": str(e)}
            else:
                # M365 failed — mark as PROBLEM but keep tenant link
                m365_error = result["steps"].get("m365_removal", {}).get("error", "Unknown error")
                m365_attempts = result["steps"].get("m365_removal", {}).get("attempts", 1)
                try:
                    db_domain.status = DomainStatus.PROBLEM
                    db_domain.error_message = (
                        f"M365 removal failed via CSV after {m365_attempts} attempt(s): {m365_error} "
                        f"({datetime.utcnow().isoformat()})"
                    )
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    logger.error(f"[{domain_name}] Could not update domain status to PROBLEM: {e}")
                result["steps"]["database_update"] = {
                    "success": True,
                    "note": "Domain marked as PROBLEM — tenant link preserved for retry",
                    "m365_failed": True
                }
        else:
            result["steps"]["database_update"] = {
                "skipped": True,
                "note": "Domain not in database"
            }
        
        result["success"] = m365_ok
        result["completed_at"] = datetime.utcnow().isoformat()
        return result

    @staticmethod
    def _reset_license_state(domain, skip_m365=False):
        # Preserve the object ID for a later real cleanup if M365 was skipped.
        if skip_m365:
            return
        domain.licensed_user_created = False
        domain.licensed_user_upn = None
        domain.licensed_user_id = None
        domain.licensed_user_password = None
        domain.domain_added_to_m365 = False
        domain.domain_verified_in_m365 = False
        domain.domain_verified_at = None
    
    # =========================================================
    # BULK OPERATIONS
    # =========================================================
    
    async def bulk_remove_from_db(
        self,
        db: AsyncSession,
        domain_names: List[str],
        skip_m365: bool = False,
        headless: bool = True,
        stagger_seconds: int = 10
    ) -> Dict[str, Any]:
        """Remove multiple domains from DB sequentially with staggering."""
        results = []
        successful = 0
        failed = 0
        
        for i, dn in enumerate(domain_names):
            r = await self.remove_domain_from_db(
                db=db, domain_name=dn, skip_m365=skip_m365, headless=headless
            )
            results.append(r)
            if r["success"]:
                successful += 1
            else:
                failed += 1
            
            if i < len(domain_names) - 1:
                await asyncio.sleep(stagger_seconds)
        
        return {
            "total": len(domain_names),
            "successful": successful,
            "failed": failed,
            "results": results
        }
    
    async def bulk_remove_from_csv(
        self,
        entries: List[Dict[str, str]],
        db: AsyncSession = None,
        skip_m365: bool = False,
        headless: bool = True,
        stagger_seconds: int = 10
    ) -> Dict[str, Any]:
        """Remove multiple CSV domains sequentially with staggering."""
        results = []
        successful = 0
        failed = 0
        
        for i, entry in enumerate(entries):
            r = await self.remove_domain_from_csv(
                entry=entry, db=db, skip_m365=skip_m365, headless=headless
            )
            results.append(r)
            if r["success"]:
                successful += 1
            else:
                failed += 1
            
            if i < len(entries) - 1:
                await asyncio.sleep(stagger_seconds)
        
        return {
            "total": len(entries),
            "successful": successful,
            "failed": failed,
            "results": results
        }


# Singleton instance
domain_removal_service = DomainRemovalService()
