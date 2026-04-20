"""
End-of-batch (and on-demand) reconciliation: verify & repair Security
Defaults and SMTP Auth state for every tenant in a batch.

ARCHITECTURE
------------
- SD verification:  Microsoft Graph API via ROPC (primary). Sub-3s per tenant.
                    Selenium (SecurityDefaultsDisabler) ONLY falls back when
                    ROPC token minting fails — and is imported *inline* in
                    that branch so a Graph-only run pays zero Selenium cost.
- SMTP verification: ExchangeOnline PowerShell (existing smtp_auth_fix).

In-memory job state lives in reconciliation_jobs (mirrors pipeline_jobs).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy import select

from app.db.session import SessionLocal as async_session_factory
from app.models.tenant import Tenant

# Graph-first SD verify/repair. This import is cheap (httpx).
from app.services.sd_graph import verify_or_repair_sd

# SMTP verify/repair via PowerShell ExchangeOnline
from app.services.smtp_auth_fix import (
    enable_smtp_auth_with_powershell,
    find_powershell_exe,
)

logger = logging.getLogger(__name__)


# In-memory job tracker, keyed by str(batch_id). Mirrors pipeline_jobs.
reconciliation_jobs: Dict[str, Dict] = {}


def _empty_summary() -> Dict:
    return {
        "batch_id": None,
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "total_tenants": 0,
        "sd_ok": 0,
        "sd_drift_fixed": 0,
        "sd_drift_unfixable": 0,
        "smtp_ok": 0,
        "smtp_drift_fixed": 0,
        "smtp_drift_unfixable": 0,
        "errors": [],
        "tenants": [],
    }


def _tenant_token_domain(tenant: Tenant) -> str:
    """
    Determine the tenant_domain path segment to use in the OAuth token URL.

    Prefers an explicit ms_tenant_name / onmicrosoft_domain, falling back
    to deriving from the admin_email.
    """
    ms_tenant_name = getattr(tenant, "ms_tenant_name", None) or getattr(
        tenant, "onmicrosoft_domain", None
    )
    if ms_tenant_name:
        return ms_tenant_name

    admin_email = getattr(tenant, "admin_email", "") or ""
    if "@" in admin_email:
        td = admin_email.split("@", 1)[1]
        if td:
            return td

    # Final fallback: the tenant's custom domain (may not work for token mint
    # but we have to return *something*)
    return getattr(tenant, "custom_domain", None) or getattr(tenant, "name", "") or ""


async def _load_batch_tenants(batch_id) -> List[Tenant]:
    """Load all tenants for a batch with credentials needed for reconciliation."""
    async with async_session_factory() as db:
        res = await db.execute(
            select(Tenant).where(Tenant.batch_id == batch_id)
        )
        return list(res.scalars().all())


async def _reconcile_sd_for_tenant(
    tenant: Tenant,
    summary: Dict,
    auto_fix: bool,
) -> Dict:
    """
    Reconcile Security Defaults for one tenant.

    Returns a per-tenant result dict that is also appended to summary.tenants.
    """
    domain = tenant.custom_domain or tenant.onmicrosoft_domain or str(tenant.id)
    t_result: Dict = {
        "tenant_id": str(tenant.id),
        "domain": domain,
        "sd": {"action": None, "sd_disabled": None, "error": None},
        "smtp": {"action": None, "enabled": None, "error": None},
    }

    try:
        tenant_domain_for_token = _tenant_token_domain(tenant)

        sd_result = await verify_or_repair_sd(
            tenant_domain=tenant_domain_for_token,
            admin_email=tenant.admin_email,
            admin_password=tenant.admin_password,
            auto_fix=auto_fix,
        )

        actual_sd_disabled = sd_result.get("sd_disabled")
        action = sd_result.get("action")

        t_result["sd"]["action"] = action
        t_result["sd"]["sd_disabled"] = actual_sd_disabled
        t_result["sd"]["error"] = sd_result.get("error")

        if action == "token_failed":
            # Graph unavailable — ROPC blocked means SD is probably still enabled.
            # Fall back to Selenium for this tenant.
            logger.info(
                "[%s] Graph token failed, falling back to Selenium for SD", domain
            )

            # Inline imports so a Graph-only happy-path pays zero Selenium cost
            from app.services.step8_security_defaults import (  # noqa: WPS433
                SecurityDefaultsDisabler,
                TenantCredentials as SDCreds,
            )

            worker_id = int(str(tenant.id).replace("-", "")[:6], 16) % 10000
            selenium_disabler = SecurityDefaultsDisabler(
                headless=True, worker_id=worker_id
            )
            creds = SDCreds(
                tenant_id=str(tenant.id),
                domain=domain,
                admin_email=tenant.admin_email,
                admin_password=tenant.admin_password,
                totp_secret=tenant.totp_secret or "",
            )

            if auto_fix:
                fix_result = await asyncio.to_thread(
                    selenium_disabler.disable_for_tenant, creds
                )
                if fix_result.get("success"):
                    summary["sd_drift_fixed"] += 1
                    t_result["sd"]["action"] = "selenium_repaired"
                    t_result["sd"]["sd_disabled"] = True
                    async with async_session_factory() as db:
                        t = await db.get(Tenant, tenant.id)
                        if t:
                            t.security_defaults_disabled = True
                            t.security_defaults_disabled_at = datetime.utcnow()
                            await db.commit()
                else:
                    summary["sd_drift_unfixable"] += 1
                    t_result["sd"]["action"] = "selenium_failed"
                    t_result["sd"]["error"] = fix_result.get("error")
                    summary["errors"].append(
                        {
                            "domain": domain,
                            "stage": "sd_selenium_fallback",
                            "error": fix_result.get("error"),
                        }
                    )
            else:
                # Verify-only mode — use the read-only Selenium verifier
                verify_result = await asyncio.to_thread(
                    selenium_disabler.verify_sd_disabled, creds
                )
                if verify_result.get("sd_disabled") is True:
                    summary["sd_ok"] += 1
                    t_result["sd"]["action"] = "selenium_verified_ok"
                    t_result["sd"]["sd_disabled"] = True
                else:
                    summary["sd_drift_unfixable"] += 1
                    t_result["sd"]["action"] = "selenium_verify_failed"
                    t_result["sd"]["error"] = verify_result.get("error")

        elif actual_sd_disabled is True and action == "already_disabled":
            summary["sd_ok"] += 1
            async with async_session_factory() as db:
                t = await db.get(Tenant, tenant.id)
                if t and not t.security_defaults_disabled:
                    t.security_defaults_disabled = True
                    t.security_defaults_disabled_at = datetime.utcnow()
                    await db.commit()

        elif action == "repaired":
            summary["sd_drift_fixed"] += 1
            async with async_session_factory() as db:
                t = await db.get(Tenant, tenant.id)
                if t:
                    t.security_defaults_disabled = True
                    t.security_defaults_disabled_at = datetime.utcnow()
                    await db.commit()

        elif action == "drift_detected":
            # auto_fix=False and SD is enabled
            summary["sd_drift_unfixable"] += 1

        else:
            # unfixable / unknown
            summary["sd_drift_unfixable"] += 1
            summary["errors"].append(
                {"domain": domain, "stage": "sd_graph", "error": sd_result.get("error")}
            )

    except Exception as e:
        logger.exception("[%s] SD reconcile exception", domain)
        summary["errors"].append(
            {"domain": domain, "stage": "sd_verify", "error": str(e)}
        )
        t_result["sd"]["error"] = str(e)

    return t_result


async def _reconcile_smtp_for_tenant(
    tenant: Tenant,
    summary: Dict,
    auto_fix: bool,
    ps_exe_state: Dict[str, Optional[str]],
    ps_exe_lock: asyncio.Lock,
    t_result: Dict,
) -> None:
    """Verify SMTP Auth; if disabled and auto_fix=True, repair via PowerShell."""
    domain = t_result["domain"]
    try:
        # First verify
        verify_result = await enable_smtp_auth_with_powershell(
            admin_email=tenant.admin_email,
            admin_password=tenant.admin_password,
            ps_exe_state=ps_exe_state,
            ps_exe_lock=ps_exe_lock,
            verify_only=True,
        )

        enabled = bool(verify_result.get("smtp_auth_enabled"))
        t_result["smtp"]["enabled"] = enabled

        if enabled:
            summary["smtp_ok"] += 1
            t_result["smtp"]["action"] = "verified_ok"
            return

        if not auto_fix:
            summary["smtp_drift_unfixable"] += 1
            t_result["smtp"]["action"] = "drift_detected"
            t_result["smtp"]["error"] = verify_result.get("error") or "SMTP auth disabled"
            return

        # Repair
        fix_result = await enable_smtp_auth_with_powershell(
            admin_email=tenant.admin_email,
            admin_password=tenant.admin_password,
            ps_exe_state=ps_exe_state,
            ps_exe_lock=ps_exe_lock,
            verify_only=False,
        )

        if fix_result.get("smtp_auth_enabled"):
            summary["smtp_drift_fixed"] += 1
            t_result["smtp"]["action"] = "repaired"
            t_result["smtp"]["enabled"] = True
        else:
            summary["smtp_drift_unfixable"] += 1
            t_result["smtp"]["action"] = "unfixable"
            t_result["smtp"]["error"] = fix_result.get("error")
            summary["errors"].append(
                {
                    "domain": domain,
                    "stage": "smtp_repair",
                    "error": fix_result.get("error"),
                }
            )

    except Exception as e:
        logger.exception("[%s] SMTP reconcile exception", domain)
        summary["errors"].append(
            {"domain": domain, "stage": "smtp_verify", "error": str(e)}
        )
        t_result["smtp"]["error"] = str(e)


async def reconcile_batch(batch_id, auto_fix: bool = True) -> Dict:
    """
    Reconcile SD + SMTP state for every tenant in a batch.

    Args:
        batch_id: UUID or str of the batch
        auto_fix: if True, repair detected drift; if False, verify-only

    Returns a summary dict (also stored in reconciliation_jobs[str(batch_id)]).
    """
    summary = _empty_summary()
    summary["batch_id"] = str(batch_id)
    summary["auto_fix"] = auto_fix
    reconciliation_jobs[str(batch_id)] = summary

    logger.info(
        "Starting reconciliation for batch %s (auto_fix=%s)", batch_id, auto_fix
    )

    try:
        tenants = await _load_batch_tenants(batch_id)
        summary["total_tenants"] = len(tenants)

        if not tenants:
            logger.info("No tenants in batch %s — nothing to reconcile", batch_id)
            summary["status"] = "completed"
            summary["completed_at"] = datetime.utcnow().isoformat()
            return summary

        # Resolve PowerShell executable once for the whole batch
        ps_exe = await asyncio.to_thread(find_powershell_exe, True)
        ps_exe_state: Dict[str, Optional[str]] = {"exe": ps_exe}
        ps_exe_lock = asyncio.Lock()

        for idx, tenant in enumerate(tenants, start=1):
            domain = (
                tenant.custom_domain
                or tenant.onmicrosoft_domain
                or str(tenant.id)
            )
            logger.info(
                "[%s] Reconciling (%d/%d)", domain, idx, len(tenants)
            )

            # SD first
            t_result = await _reconcile_sd_for_tenant(
                tenant=tenant,
                summary=summary,
                auto_fix=auto_fix,
            )

            # Then SMTP
            await _reconcile_smtp_for_tenant(
                tenant=tenant,
                summary=summary,
                auto_fix=auto_fix,
                ps_exe_state=ps_exe_state,
                ps_exe_lock=ps_exe_lock,
                t_result=t_result,
            )

            summary["tenants"].append(t_result)

            # Graph is fast — a short pause keeps us under AAD rate limits
            await asyncio.sleep(1)

        summary["status"] = "completed"
        summary["completed_at"] = datetime.utcnow().isoformat()
        logger.info(
            "Reconciliation for batch %s done: sd_ok=%d fixed=%d unfixable=%d | "
            "smtp_ok=%d fixed=%d unfixable=%d | errors=%d",
            batch_id,
            summary["sd_ok"],
            summary["sd_drift_fixed"],
            summary["sd_drift_unfixable"],
            summary["smtp_ok"],
            summary["smtp_drift_fixed"],
            summary["smtp_drift_unfixable"],
            len(summary["errors"]),
        )
        return summary

    except Exception as e:
        logger.exception("Reconciliation for batch %s crashed", batch_id)
        summary["status"] = "error"
        summary["completed_at"] = datetime.utcnow().isoformat()
        summary["errors"].append(
            {"stage": "reconcile_batch", "error": str(e)}
        )
        return summary
