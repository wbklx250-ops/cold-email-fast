"""
Objective batch reconciliation.

This pass treats local DB flags as cache only. It checks Microsoft Graph,
Exchange Online, and Cloudflare directly, then updates DB flags from observed
truth and repairs only missing/incorrect pieces.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

import httpx
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import async_session_factory
from app.models.batch import SetupBatch
from app.models.domain import Domain, DomainStatus
from app.models.mailbox import Mailbox
from app.models.tenant import Tenant, TenantStatus
from app.services.batch_reconciliation import reconcile_batch
from app.services.cloudflare import cloudflare_service
from app.services.email_generator import MAILBOX_PASSWORD
from app.services.step7_fast import (
    _ps_escape,
    _run_powershell,
    build_expected_mailbox_data,
    process_domain_fast,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

objective_reconciliation_jobs: Dict[str, Dict[str, Any]] = {}


async def _get_objective_graph_token(
    tenant_domain: str,
    admin_email: str,
    admin_password: str,
) -> Optional[Dict[str, Any]]:
    """Mint a Graph token from tenant credentials.

    Prefer the configured app, but fall back to Microsoft's public PowerShell
    client. Some historical tenants have not consented to our app, while the
    public client can still provide objective Graph domain truth.
    """
    settings = get_settings()
    token_url = f"https://login.microsoftonline.com/{tenant_domain}/oauth2/v2.0/token"

    token_attempts: List[tuple[str, dict]] = []
    configured_client_id = settings.azure_client_id or settings.MS_CLIENT_ID
    if configured_client_id:
        configured_data = {
            "grant_type": "password",
            "client_id": configured_client_id,
            "scope": "https://graph.microsoft.com/.default",
            "username": admin_email,
            "password": admin_password,
        }
        if settings.azure_client_secret:
            configured_data["client_secret"] = settings.azure_client_secret
        token_attempts.append(("configured_app", configured_data))

    token_attempts.append(
        (
            "azure_powershell_client",
            {
                "grant_type": "password",
                "client_id": "1b730954-1685-4b74-9bfd-dac224a7b894",
                "scope": "https://graph.microsoft.com/.default",
                "username": admin_email,
                "password": admin_password,
            },
        )
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        for label, data in token_attempts:
            try:
                resp = await client.post(token_url, data=data)
            except Exception as exc:
                logger.warning(
                    "Objective Graph token exception for %s via %s: %s",
                    admin_email,
                    label,
                    exc,
                )
                continue

            if resp.status_code == 200:
                body = resp.json()
                if label != "configured_app":
                    logger.info("Objective Graph token using %s for %s", label, admin_email)
                return {
                    "access_token": body["access_token"],
                    "expires_in": body.get("expires_in", 3600),
                }

            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text}
            logger.warning(
                "Objective Graph token failed for %s via %s: %s %s",
                admin_email,
                label,
                resp.status_code,
                (body.get("error_description") or body.get("error") or str(body))[:300],
            )

    return None


def _empty_summary(batch_id) -> Dict[str, Any]:
    return {
        "batch_id": str(batch_id),
        "mode": "full",
        "status": "running",
        "started_at": datetime.utcnow().isoformat(),
        "completed_at": None,
        "auto_fix": None,
        "total_domains": 0,
        "domains_checked": 0,
        "m365_verified": 0,
        "dkim_ok": 0,
        "mailboxes_ok": 0,
        "repaired": 0,
        "failed": 0,
        "errors": [],
        "domains": [],
    }


def _tenant_token_domain(tenant_data: Dict[str, Any]) -> str:
    for key in ("onmicrosoft_domain", "ms_tenant_name"):
        value = tenant_data.get(key)
        if value:
            return value
    admin_email = tenant_data.get("admin_email") or ""
    if "@" in admin_email:
        return admin_email.split("@", 1)[1]
    return tenant_data.get("custom_domain") or tenant_data.get("name") or ""


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).lower() for item in value if item]
    if isinstance(value, str) and value:
        return [value.lower()]
    return []


async def _graph_request(
    access_token: str,
    method: str,
    endpoint: str,
    json_data: Optional[dict] = None,
) -> tuple[int, dict]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method,
            f"{GRAPH_BASE}{endpoint}",
            headers=headers,
            json=json_data,
        )
    if resp.status_code == 204 or not resp.content:
        return resp.status_code, {}
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text}


async def _get_graph_domain(access_token: str, domain: str) -> Optional[dict]:
    status, data = await _graph_request(access_token, "GET", f"/domains/{domain}")
    if status == 404:
        return None
    if status >= 400:
        raise RuntimeError(data.get("error", {}).get("message") or str(data))
    return data


async def _repair_m365_domain_with_selenium(
    domain_data: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    """Fallback for tenants where Graph app consent is unavailable."""
    domain = domain_data["name"]
    tenant = domain_data["tenant"]
    result = {
        "ok": False,
        "domain_exists": False,
        "verified": False,
        "token_ok": False,
        "access_token": None,
        "verification_txt": None,
        "action": "selenium_fallback",
        "error": reason,
    }

    try:
        from app.services.m365_setup import (
            STEP6_DOMAIN_TIMEOUT_SECONDS,
            _save_step6_result,
            _sync_setup_domain,
        )
        from app.services.selenium.browser import kill_all_browsers

        zone_id = domain_data.get("cloudflare_zone_id")
        if not zone_id:
            zone = await cloudflare_service.get_zone_by_name(domain)
            zone_id = zone.get("zone_id") if zone else None
        if not zone_id:
            result["error"] = f"{reason}; no Cloudflare zone available for Selenium setup"
            return result

        selenium_input = {
            "tenant_id": str(tenant["id"]),
            "domain_id": str(domain_data["id"]),
            "domain": domain,
            "zone_id": zone_id,
            "admin_email": tenant["admin_email"],
            "admin_password": tenant["admin_password"],
            "totp_secret": tenant.get("totp_secret"),
        }

        logger.info("[%s] Graph unavailable; running Selenium M365 setup fallback", domain)
        future = asyncio.get_event_loop().run_in_executor(
            None,
            _sync_setup_domain,
            selenium_input,
        )
        try:
            selenium_result = await asyncio.wait_for(
                future,
                timeout=STEP6_DOMAIN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            kill_all_browsers()
            selenium_result = {
                "success": False,
                "verified": False,
                "dns_configured": False,
                "error": f"Selenium M365 setup timed out after {STEP6_DOMAIN_TIMEOUT_SECONDS}s",
            }

        await _save_step6_result(selenium_input, selenium_result)
        verified = bool(selenium_result.get("success") or selenium_result.get("verified"))
        result.update(
            {
                "ok": verified,
                "domain_exists": verified,
                "verified": verified,
                "verification_txt": selenium_result.get("verification_txt"),
                "action": "selenium_repaired" if verified else "selenium_failed",
                "error": None if verified else selenium_result.get("error") or reason,
            }
        )
        return result
    except Exception as exc:
        logger.exception("[%s] Selenium M365 fallback failed", domain)
        result["action"] = "selenium_exception"
        result["error"] = f"{reason}; Selenium fallback failed: {exc}"
        return result


async def _get_verification_txt(access_token: str, domain: str) -> Optional[str]:
    status, data = await _graph_request(
        access_token,
        "GET",
        f"/domains/{domain}/verificationDnsRecords",
    )
    if status >= 400:
        raise RuntimeError(data.get("error", {}).get("message") or str(data))
    for record in data.get("value", []):
        if record.get("recordType") == "Txt" and record.get("text"):
            return record["text"]
    return None


async def _verify_m365_without_graph(
    domain_data: Dict[str, Any],
    reason: str,
    auto_fix: bool,
) -> Dict[str, Any]:
    """
    If Graph app consent is missing, use Exchange as objective evidence first.

    A DKIM signing config can only be read/created for a domain accepted by the
    tenant. That is enough to avoid expensive Selenium for already-attached
    domains, while still falling back to Selenium for domains that are not in
    Exchange.
    """
    domain = domain_data["name"]
    result = {
        "ok": False,
        "domain_exists": False,
        "verified": False,
        "token_ok": False,
        "access_token": None,
        "verification_txt": None,
        "action": "graph_unavailable",
        "error": reason,
    }

    dkim_probe = await _read_dkim_truth(
        domain_data,
        create=False,
        enable=False,
    )
    result["dkim_probe"] = {
        "success": dkim_probe.get("success"),
        "exists": dkim_probe.get("exists"),
        "enabled": dkim_probe.get("enabled"),
        "accepted_domain_exists": dkim_probe.get("accepted_domain_exists"),
        "error": dkim_probe.get("error"),
    }

    if dkim_probe.get("accepted_domain_exists") or dkim_probe.get("exists"):
        logger.info("[%s] Exchange confirmed domain exists", domain)
        result.update(
            {
                "ok": True,
                "domain_exists": True,
                "verified": True,
                "action": "exchange_dkim_confirmed",
                "error": None,
            }
        )
        return result

    if auto_fix:
        return await _repair_m365_domain_with_selenium(domain_data, reason)

    return result


async def _verify_m365_domain(
    domain_data: Dict[str, Any],
    auto_fix: bool,
) -> Dict[str, Any]:
    domain = domain_data["name"]
    tenant = domain_data["tenant"]
    result = {
        "ok": False,
        "domain_exists": False,
        "verified": False,
        "token_ok": False,
        "access_token": None,
        "verification_txt": None,
        "action": "checked",
        "error": None,
    }

    token_payload = await _get_objective_graph_token(
        tenant_domain=_tenant_token_domain(tenant),
        admin_email=tenant["admin_email"],
        admin_password=tenant["admin_password"],
    )
    if not token_payload:
        return await _verify_m365_without_graph(
            domain_data,
            "Could not mint Graph token from tenant credentials",
            auto_fix,
        )

    access_token = token_payload["access_token"]
    result["token_ok"] = True
    result["access_token"] = access_token

    try:
        graph_domain = await _get_graph_domain(access_token, domain)
    except RuntimeError as exc:
        return await _verify_m365_without_graph(domain_data, str(exc), auto_fix)
    if not graph_domain and auto_fix:
        status, data = await _graph_request(
            access_token,
            "POST",
            "/domains",
            {"id": domain},
        )
        if status not in (200, 201):
            result["action"] = "add_failed"
            result["error"] = data.get("error", {}).get("message") or str(data)
            return result
        result["action"] = "added"
        graph_domain = await _get_graph_domain(access_token, domain)

    result["domain_exists"] = graph_domain is not None
    result["verified"] = bool(graph_domain and graph_domain.get("isVerified"))

    if result["verified"]:
        result["ok"] = True
        return result

    if not auto_fix or not graph_domain:
        result["error"] = "Domain is not verified in this tenant"
        return result

    txt_value = await _get_verification_txt(access_token, domain)
    result["verification_txt"] = txt_value
    if txt_value:
        zone_id = domain_data.get("cloudflare_zone_id")
        if not zone_id:
            zone = await cloudflare_service.get_zone_by_name(domain)
            zone_id = zone.get("zone_id") if zone else None
        if zone_id:
            await cloudflare_service.ensure_txt_record(
                zone_id=zone_id,
                name="@",
                content=txt_value,
                domain=domain,
            )
            await asyncio.sleep(5)

    status, data = await _graph_request(access_token, "POST", f"/domains/{domain}/verify")
    if status not in (200, 201, 204):
        result["action"] = "verify_failed"
        result["error"] = data.get("error", {}).get("message") or str(data)
        return result

    graph_domain = await _get_graph_domain(access_token, domain)
    result["verified"] = bool(graph_domain and graph_domain.get("isVerified"))
    result["ok"] = result["verified"]
    result["action"] = "verified" if result["ok"] else "verify_incomplete"
    if not result["ok"]:
        result["error"] = "Graph verify returned but domain is still not verified"
    return result


def _build_dkim_script(
    admin_email: str,
    admin_password: str,
    domain: str,
    create: bool = False,
    enable: bool = False,
) -> str:
    template = r'''
$ErrorActionPreference = "Stop"
$out = @{
    success = $false
    exists = $false
    enabled = $false
    selector1 = $null
    selector2 = $null
    accepted_domain_exists = $false
    error = $null
}
function Find-DkimConfig([string]$DomainName) {
    $cfg = Get-DkimSigningConfig -Identity $DomainName -ErrorAction SilentlyContinue
    if (-not $cfg) {
        $cfg = Get-DkimSigningConfig -ErrorAction SilentlyContinue |
            Where-Object {
                ([string]$_.Domain -eq $DomainName) -or
                ([string]$_.Identity -eq $DomainName) -or
                ([string]$_.Name -eq $DomainName)
            } |
            Select-Object -First 1
    }
    return $cfg
}
try {
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    $sp = ConvertTo-SecureString '__PASSWORD_SINGLE__' -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential('__ADMIN__', $sp)
    Connect-ExchangeOnline -Credential $cred -ShowBanner:$false -ErrorAction Stop

    $accepted = Get-AcceptedDomain -Identity '__DOMAIN__' -ErrorAction SilentlyContinue
    if ($accepted) {
        $out.accepted_domain_exists = $true
    }

    $dkim = Find-DkimConfig '__DOMAIN__'
    if (__CREATE__ -and (-not $dkim -or -not $dkim.Selector1CNAME -or -not $dkim.Selector2CNAME)) {
        try {
            New-DkimSigningConfig -DomainName '__DOMAIN__' -Enabled:$false -ErrorAction Stop | Out-Null
        } catch {
            if ($_.Exception.Message -notmatch "already|exist") {
                throw
            }
        }
        Start-Sleep -Seconds 2
        $dkim = Find-DkimConfig '__DOMAIN__'
    }

    if ($dkim -and __ENABLE__ -and -not $dkim.Enabled) {
        $identity = if ($dkim.Identity) { [string]$dkim.Identity } else { '__DOMAIN__' }
        try {
            Set-DkimSigningConfig -Identity $identity -Enabled:$true -ErrorAction Stop
        } catch {
            Set-DkimSigningConfig -Identity '__DOMAIN__' -Enabled:$true -ErrorAction Stop
        }
        Start-Sleep -Seconds 2
        $dkim = Find-DkimConfig '__DOMAIN__'
    }

    if ($dkim) {
        $out.exists = $true
        $out.enabled = [bool]$dkim.Enabled
        $out.selector1 = $dkim.Selector1CNAME
        $out.selector2 = $dkim.Selector2CNAME
    }
    $out.success = $true
} catch {
    $out.error = $_.Exception.Message
} finally {
    try { Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue } catch {}
}
$out | ConvertTo-Json -Compress
'''
    return (
        template
        .replace("__ADMIN__", _ps_escape(admin_email))
        .replace("__PASSWORD_SINGLE__", admin_password.replace("'", "''"))
        .replace("__DOMAIN__", _ps_escape(domain))
        .replace("__CREATE__", "$true" if create else "$false")
        .replace("__ENABLE__", "$true" if enable else "$false")
    )


async def _read_dkim_truth(
    domain_data: Dict[str, Any],
    create: bool = False,
    enable: bool = False,
) -> Dict[str, Any]:
    tenant = domain_data["tenant"]
    script = _build_dkim_script(
        admin_email=tenant["admin_email"],
        admin_password=tenant["admin_password"],
        domain=domain_data["name"],
        create=create,
        enable=enable,
    )
    try:
        result = await _run_powershell(script, timeout=180)
    except Exception as exc:
        return {"success": False, "enabled": False, "error": str(exc)}
    result["ok"] = bool(result.get("success") and result.get("enabled"))
    return result


def _attach_dkim_selectors_from_error(dkim: Dict[str, Any]) -> Dict[str, Any]:
    """
    Exchange sometimes fails DKIM enablement with a human-readable message that
    includes the required CNAME targets, while leaving Selector1CNAME and
    Selector2CNAME empty. Use those targets to repair DNS, but do not treat DKIM
    itself as enabled until Exchange confirms it.
    """
    if dkim.get("selector1") and dkim.get("selector2"):
        return dkim

    error = str(dkim.get("error") or "")
    if "Points to address or value" not in error:
        return dkim

    targets = re.findall(
        r"Points to address or value:\s*([A-Za-z0-9._-]+\.dkim\.mail\.microsoft)",
        error,
        flags=re.IGNORECASE,
    )
    if len(targets) >= 2:
        dkim = dict(dkim)
        dkim["selector1"] = dkim.get("selector1") or targets[0].rstrip(".")
        dkim["selector2"] = dkim.get("selector2") or targets[1].rstrip(".")
        dkim["selector_source"] = "exchange_error"
    return dkim


async def _ensure_cloudflare_truth(
    domain_data: Dict[str, Any],
    dkim_truth: Optional[Dict[str, Any]],
    auto_fix: bool,
) -> Dict[str, Any]:
    domain = domain_data["name"]
    result = {
        "zone_ok": False,
        "zone_id": domain_data.get("cloudflare_zone_id"),
        "mx": False,
        "spf": False,
        "autodiscover": False,
        "dkim1": False,
        "dkim2": False,
        "error": None,
    }
    try:
        zone = None
        if result["zone_id"]:
            zone = await cloudflare_service.get_zone_by_id(result["zone_id"])
        if not zone:
            zone = await cloudflare_service.get_zone_by_name(domain)
        if not zone and auto_fix:
            zone = await cloudflare_service.get_or_create_zone(domain)

        if not zone:
            result["error"] = "Cloudflare zone not found"
            return result

        result["zone_ok"] = True
        result["zone_id"] = zone.get("zone_id")

        if auto_fix:
            await cloudflare_service.ensure_email_dns_records(result["zone_id"], domain)
            if dkim_truth and dkim_truth.get("selector1") and dkim_truth.get("selector2"):
                await cloudflare_service.ensure_dkim_cnames(
                    result["zone_id"],
                    domain,
                    dkim_truth["selector1"],
                    dkim_truth["selector2"],
                )

        records = await cloudflare_service.list_dns_records(result["zone_id"])

        def _matches_name(record: dict, name: str) -> bool:
            record_name = (record.get("name") or "").rstrip(".").lower()
            if name == "@":
                return record_name == domain.lower()
            return record_name == name.lower() or record_name == f"{name.lower()}.{domain.lower()}"

        mx = next(
            (
                r for r in records
                if r.get("type") == "MX" and _matches_name(r, "@")
                and "mail.protection.outlook.com" in (r.get("content") or "")
            ),
            None,
        )
        spf = next(
            (
                r for r in records
                if r.get("type") == "TXT" and _matches_name(r, "@")
                and "include:spf.protection.outlook.com" in (r.get("content") or "")
            ),
            None,
        )
        autodiscover = next(
            (
                r for r in records
                if r.get("type") == "CNAME" and _matches_name(r, "autodiscover")
                and (r.get("content") or "").rstrip(".").lower() == "autodiscover.outlook.com"
            ),
            None,
        )
        result["mx"] = bool(mx and "mail.protection.outlook.com" in (mx.get("content") or ""))
        result["spf"] = bool(spf and "include:spf.protection.outlook.com" in (spf.get("content") or ""))
        result["autodiscover"] = bool(
            autodiscover and (autodiscover.get("content") or "").rstrip(".").lower() == "autodiscover.outlook.com"
        )

        if dkim_truth and dkim_truth.get("selector1") and dkim_truth.get("selector2"):
            dk1 = next(
                (
                    r for r in records
                    if r.get("type") == "CNAME" and _matches_name(r, "selector1._domainkey")
                    and dkim_truth["selector1"].rstrip(".").lower()
                    in (r.get("content") or "").rstrip(".").lower()
                ),
                None,
            )
            dk2 = next(
                (
                    r for r in records
                    if r.get("type") == "CNAME" and _matches_name(r, "selector2._domainkey")
                    and dkim_truth["selector2"].rstrip(".").lower()
                    in (r.get("content") or "").rstrip(".").lower()
                ),
                None,
            )
            result["dkim1"] = bool(
                dk1 and (dkim_truth["selector1"].rstrip(".").lower() in (dk1.get("content") or "").rstrip(".").lower())
            )
            result["dkim2"] = bool(
                dk2 and (dkim_truth["selector2"].rstrip(".").lower() in (dk2.get("content") or "").rstrip(".").lower())
            )
    except Exception as exc:
        result["error"] = str(exc)
    return result


async def _repair_dkim_if_needed(
    domain_data: Dict[str, Any],
    auto_fix: bool,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    dkim = _attach_dkim_selectors_from_error(await _read_dkim_truth(domain_data))
    cf = await _ensure_cloudflare_truth(domain_data, dkim, auto_fix=auto_fix)
    if dkim.get("ok") or not auto_fix:
        return dkim, cf

    dkim = _attach_dkim_selectors_from_error(
        await _read_dkim_truth(domain_data, create=True, enable=False)
    )
    cf = await _ensure_cloudflare_truth(domain_data, dkim, auto_fix=True)
    if dkim.get("selector1") and dkim.get("selector2"):
        await asyncio.sleep(30)
        dkim = _attach_dkim_selectors_from_error(
            await _read_dkim_truth(domain_data, create=True, enable=True)
        )
    cf = await _ensure_cloudflare_truth(domain_data, dkim, auto_fix=True)
    return dkim, cf


def _build_mailbox_truth_script(
    admin_email: str,
    admin_password: str,
    domain: str,
    expected_mailboxes: List[Dict[str, str]],
) -> str:
    entries = []
    for mb in expected_mailboxes:
        entries.append(f'    @{{ Email="{_ps_escape(mb["email"])}" }}')
    mailbox_array = ",\n".join(entries)
    template = r'''
$ErrorActionPreference = "Continue"
$out = @{
    success = $false
    all_ok = $false
    licensed_user = "me1@__DOMAIN__"
    licensed_user_exists = $false
    licensed_user_has_allowed_license = $false
    license_sku = $null
    expected = 0
    existing = 0
    full_access = 0
    send_as = 0
    account_enabled = 0
    missing = @()
    missing_full_access = @()
    missing_send_as = @()
    disabled_accounts = @()
    error = $null
}
try {
    Import-Module ExchangeOnlineManagement -ErrorAction Stop
    Import-Module Microsoft.Graph.Users -ErrorAction Stop
    Import-Module Microsoft.Graph.Identity.DirectoryManagement -ErrorAction SilentlyContinue

    $sp = ConvertTo-SecureString '__PASSWORD_SINGLE__' -AsPlainText -Force
    $cred = New-Object System.Management.Automation.PSCredential('__ADMIN__', $sp)
    Connect-ExchangeOnline -Credential $cred -ShowBanner:$false -ErrorAction Stop

    $body = @{
        grant_type = "password"
        client_id = "1b730954-1685-4b74-9bfd-dac224a7b894"
        scope = "https://graph.microsoft.com/.default"
        username = "__ADMIN__"
        password = "__PASSWORD_DOUBLE__"
    }
    $td = "__ADMIN__".Split("@")[1]
    $tok = Invoke-RestMethod -Method Post -Uri "https://login.microsoftonline.com/$td/oauth2/v2.0/token" -Body $body -ErrorAction Stop
    $sec = ConvertTo-SecureString $tok.access_token -AsPlainText -Force
    Connect-MgGraph -AccessToken $sec -NoWelcome -ErrorAction Stop

    $licensedUser = "me1@__DOMAIN__"
    $licensedLocal = $licensedUser.Split("@")[0].ToLowerInvariant()
    $delegateKeys = New-Object System.Collections.Generic.HashSet[string]
    [void]$delegateKeys.Add($licensedUser.ToLowerInvariant())
    $licensed = Get-MgUser -UserId $licensedUser -Property "AssignedLicenses" -ErrorAction SilentlyContinue
    if ($licensed) {
        $out.licensed_user_exists = $true
        $allowedSkuPartNumbers = @(
            "O365_BUSINESS_ESSENTIALS",
            "SMB_BUSINESS_ESSENTIALS",
            "Microsoft_365_Business_Basic_(no Teams)",
            "Microsoft_365_Business_Basic_(no_Teams)",
            "Microsoft_365_Business_Basic_EEA_(no_Teams)",
            "Microsoft_365_Business_Basic_EEA_(no Teams)",
            "EXCHANGESTANDARD"
        )
        $allSkus = Get-MgSubscribedSku -ErrorAction SilentlyContinue
        $assignedSkuIds = @($licensed.AssignedLicenses | ForEach-Object { [string]$_.SkuId })
        $matchedSku = $allSkus | Where-Object {
            ($allowedSkuPartNumbers -contains $_.SkuPartNumber) -and
            ($assignedSkuIds -contains ([string]$_.SkuId))
        } | Select-Object -First 1
        if ($matchedSku) {
            $out.licensed_user_has_allowed_license = $true
            $out.license_sku = $matchedSku.SkuPartNumber
        }
    }
    $delegateRecipient = Get-Recipient -Identity $licensedUser -ErrorAction SilentlyContinue
    if ($delegateRecipient) {
        foreach ($candidate in @(
            $delegateRecipient.PrimarySmtpAddress,
            $delegateRecipient.WindowsEmailAddress,
            $delegateRecipient.UserPrincipalName,
            $delegateRecipient.Alias,
            $delegateRecipient.Name,
            $delegateRecipient.DisplayName,
            $delegateRecipient.DistinguishedName
        )) {
            if ($candidate) {
                [void]$delegateKeys.Add(([string]$candidate).ToLowerInvariant())
            }
        }
    }

    $mailboxes = @(
__MAILBOX_ARRAY__
    )
    $out.expected = $mailboxes.Count

    foreach ($mb in $mailboxes) {
        $email = $mb.Email
        $mailbox = Get-Mailbox -Identity $email -ErrorAction SilentlyContinue
        if (-not $mailbox) {
            $out.missing += $email
            continue
        }

        $out.existing++
        $fa = Get-MailboxPermission -Identity $email -ErrorAction SilentlyContinue |
            Where-Object {
                $deny = ([string]$_.Deny).ToLowerInvariant() -eq "true"
                $inherited = ([string]$_.IsInherited).ToLowerInvariant() -eq "true"
                if (-not ($_.AccessRights -contains "FullAccess") -or $deny -or $inherited) {
                    $false
                } else {
                    $userText = ([string]$_.User).ToLowerInvariant()
                    (
                        $delegateKeys.Contains($userText) -or
                        $userText.EndsWith("\" + $licensedLocal) -or
                        $userText -eq $licensedLocal
                    )
                }
            } |
            Select-Object -First 1
        if ($fa) {
            $out.full_access++
        } else {
            $out.missing_full_access += $email
        }

        $sa = Get-RecipientPermission -Identity $email -Trustee $licensedUser -ErrorAction SilentlyContinue |
            Where-Object {
                $deny = ([string]$_.Deny).ToLowerInvariant() -eq "true"
                ($_.AccessRights -contains "SendAs") -and (-not $deny)
            } |
            Select-Object -First 1
        if ($sa) {
            $out.send_as++
        } else {
            $out.missing_send_as += $email
        }

        $user = Get-MgUser -UserId $email -Property "AccountEnabled" -ErrorAction SilentlyContinue
        if ($user -and $user.AccountEnabled -eq $true) {
            $out.account_enabled++
        } else {
            $out.disabled_accounts += $email
        }
    }

    $out.success = $true
    $out.all_ok = (
        $out.licensed_user_exists -and
        $out.licensed_user_has_allowed_license -and
        ($out.expected -gt 0) -and
        ($out.existing -eq $out.expected) -and
        ($out.full_access -eq $out.expected) -and
        ($out.send_as -eq $out.expected) -and
        ($out.account_enabled -eq $out.expected)
    )
} catch {
    $out.error = $_.Exception.Message
} finally {
    try { Disconnect-ExchangeOnline -Confirm:$false -ErrorAction SilentlyContinue } catch {}
    try { Disconnect-MgGraph -ErrorAction SilentlyContinue } catch {}
}
$out | ConvertTo-Json -Depth 6 -Compress
'''
    return (
        template
        .replace("__ADMIN__", _ps_escape(admin_email))
        .replace("__PASSWORD_SINGLE__", admin_password.replace("'", "''"))
        .replace("__PASSWORD_DOUBLE__", _ps_escape(admin_password))
        .replace("__DOMAIN__", _ps_escape(domain))
        .replace("__MAILBOX_ARRAY__", mailbox_array)
    )


async def _verify_mailbox_truth(
    domain_data: Dict[str, Any],
    expected_mailboxes: List[Dict[str, str]],
) -> Dict[str, Any]:
    tenant = domain_data["tenant"]
    script = _build_mailbox_truth_script(
        admin_email=tenant["admin_email"],
        admin_password=tenant["admin_password"],
        domain=domain_data["name"],
        expected_mailboxes=expected_mailboxes,
    )
    try:
        truth = await _run_powershell(script, timeout=600)
    except Exception as exc:
        truth = {"success": False, "all_ok": False, "error": str(exc)}

    for key in ("missing", "missing_full_access", "missing_send_as", "disabled_accounts"):
        truth[key] = _as_list(truth.get(key))
    return truth


async def _save_domain_truth(
    domain_data: Dict[str, Any],
    m365: Dict[str, Any],
    dkim: Dict[str, Any],
    cf: Dict[str, Any],
    mailbox: Optional[Dict[str, Any]],
    expected_mailboxes: List[Dict[str, str]],
) -> None:
    domain_id = domain_data["id"]
    tenant_id = domain_data["tenant"]["id"]
    now = datetime.utcnow()

    async with async_session_factory() as db:
        domain = await db.get(Domain, domain_id)
        if domain:
            domain.domain_added_to_m365 = bool(m365.get("domain_exists"))
            domain.domain_verified_in_m365 = bool(m365.get("verified"))
            if m365.get("verified") and not domain.domain_verified_at:
                domain.domain_verified_at = now
                domain.m365_verified_at = now
            if m365.get("verification_txt"):
                domain.m365_verification_txt = m365["verification_txt"]
                domain.verification_txt_value = m365["verification_txt"]
            if cf.get("zone_id"):
                domain.cloudflare_zone_id = cf["zone_id"]
            domain.mx_record_added = bool(cf.get("mx"))
            domain.spf_record_added = bool(cf.get("spf"))
            domain.autodiscover_added = bool(cf.get("autodiscover"))
            domain.dns_records_created = bool(cf.get("mx") and cf.get("spf") and cf.get("autodiscover"))
            domain.dkim_cnames_added = bool(cf.get("dkim1") and cf.get("dkim2"))
            if dkim.get("selector1"):
                domain.dkim_selector1 = dkim["selector1"]
                domain.dkim_selector1_cname = dkim["selector1"]
            if dkim.get("selector2"):
                domain.dkim_selector2 = dkim["selector2"]
                domain.dkim_selector2_cname = dkim["selector2"]
            domain.dkim_enabled = bool(dkim.get("enabled"))
            if dkim.get("enabled") and not domain.dkim_enabled_at:
                domain.dkim_enabled_at = now
            domain.step5_complete = bool(m365.get("verified") and dkim.get("enabled"))
            domain.status = DomainStatus.ACTIVE if domain.step5_complete else DomainStatus.PROBLEM

            if mailbox:
                domain.licensed_user_created = bool(mailbox.get("licensed_user_exists"))
                domain.licensed_user_upn = mailbox.get("licensed_user") or f"me1@{domain.name}"
                domain.licensed_user_password = MAILBOX_PASSWORD
                domain.step6_complete = bool(mailbox.get("all_ok"))
                domain.step6_mailboxes_created = int(mailbox.get("existing") or 0)

            errors = [
                m365.get("error"),
                dkim.get("error"),
                cf.get("error"),
                mailbox.get("error") if mailbox else None,
            ]
            domain.error_message = "; ".join(str(e) for e in errors if e) or None

        if mailbox and expected_mailboxes:
            expected_emails = [mb["email"].lower() for mb in expected_mailboxes]
            existing = set(_as_list(mailbox.get("missing")))
            missing = set(existing)
            full_ok = set(expected_emails) - set(_as_list(mailbox.get("missing_full_access")))
            send_ok = set(expected_emails) - set(_as_list(mailbox.get("missing_send_as")))
            enabled_ok = set(expected_emails) - set(_as_list(mailbox.get("disabled_accounts")))

            rows = (await db.execute(
                select(Mailbox).where(
                    Mailbox.tenant_id == tenant_id,
                    Mailbox.email.in_(expected_emails),
                )
            )).scalars().all()
            for row in rows:
                email = row.email.lower()
                row.created_in_exchange = email not in missing
                row.delegated = email in full_ok and email in send_ok
                row.account_enabled = email in enabled_ok
                row.setup_complete = bool(
                    row.created_in_exchange and row.delegated and row.account_enabled
                )
                if row.setup_complete and not row.setup_completed_at:
                    row.setup_completed_at = now

        tenant = await db.get(Tenant, tenant_id)
        if tenant:
            if m365.get("verified"):
                tenant.domain_verified_in_m365 = True
            if dkim.get("enabled"):
                tenant.dkim_enabled = True
            remaining = await db.scalar(
                select(func.count(Domain.id)).where(
                    Domain.tenant_id == tenant_id,
                    Domain.step6_complete.is_not(True),
                    Domain.step6_skipped.is_not(True),
                )
            ) or 0
            if remaining == 0:
                tenant.step6_complete = True
                tenant.step6_completed_at = tenant.step6_completed_at or now
                tenant.status = TenantStatus.READY
                tenant.step6_error = None
            elif mailbox:
                tenant.step6_complete = False
                tenant.step6_error = "Objective reconciliation found incomplete mailbox setup"

        await db.commit()


async def _load_batch_domain_data(batch_id) -> tuple[Optional[dict], List[Dict[str, Any]]]:
    async with async_session_factory() as db:
        batch = await db.get(SetupBatch, batch_id)
        if not batch:
            return None, []
        batch_data = {
            "id": batch.id,
            "persona_first_name": batch.persona_first_name,
            "persona_last_name": batch.persona_last_name,
            "custom_mailbox_map": batch.custom_mailbox_map,
            "mailboxes_per_tenant": batch.mailboxes_per_tenant or 50,
        }
        domains = (await db.execute(
            select(Domain)
            .where(Domain.batch_id == batch_id, Domain.tenant_id.isnot(None))
            .order_by(Domain.created_at)
        )).scalars().all()

        data = []
        for domain in domains:
            tenant = await db.get(Tenant, domain.tenant_id)
            if not tenant:
                continue
            data.append(
                {
                    "id": domain.id,
                    "name": domain.name.lower(),
                    "tenant_id": domain.tenant_id,
                    "cloudflare_zone_id": domain.cloudflare_zone_id,
                    "domain_index_in_tenant": domain.domain_index_in_tenant or 0,
                    "persona_first_name": domain.persona_first_name,
                    "persona_last_name": domain.persona_last_name,
                    "step6_complete": bool(domain.step6_complete),
                    "error_message": domain.error_message,
                    "tenant": {
                        "id": tenant.id,
                        "name": tenant.name,
                        "custom_domain": tenant.custom_domain,
                        "onmicrosoft_domain": tenant.onmicrosoft_domain,
                        "admin_email": tenant.admin_email,
                        "admin_password": tenant.admin_password,
                        "totp_secret": tenant.totp_secret,
                    },
                }
            )
        return batch_data, data


def _is_active_tenant_blocker(domain_data: Dict[str, Any]) -> bool:
    return "active tenant" in (domain_data.get("error_message") or "").lower()


async def objective_reconcile_batch(
    batch_id,
    auto_fix: bool = True,
    *,
    recoverable_only: bool = False,
    final_security_smtp: bool = True,
    job_key_suffix: Optional[str] = None,
) -> Dict[str, Any]:
    summary = _empty_summary(batch_id)
    summary["auto_fix"] = auto_fix
    summary["mode"] = "recoverable" if recoverable_only else "full"
    job_key = f"{batch_id}:{job_key_suffix}" if job_key_suffix else str(batch_id)
    objective_reconciliation_jobs[job_key] = summary

    try:
        batch_data, domains = await _load_batch_domain_data(batch_id)
        if batch_data is None:
            summary["status"] = "error"
            summary["errors"].append({"stage": "load", "error": "Batch not found"})
            return summary

        if recoverable_only:
            before_count = len(domains)
            active_tenant_blockers = [
                domain for domain in domains if _is_active_tenant_blocker(domain)
            ]
            domains = [
                domain for domain in domains
                if not domain.get("step6_complete")
                and not _is_active_tenant_blocker(domain)
            ]
            summary["selection"] = {
                "source_domains": before_count,
                "excluded_already_complete": before_count - len(domains) - len(active_tenant_blockers),
                "excluded_active_tenant_blockers": len(active_tenant_blockers),
            }

        summary["total_domains"] = len(domains)

        for idx, domain_data in enumerate(domains, start=1):
            domain_name = domain_data["name"]
            domain_result: Dict[str, Any] = {
                "domain": domain_name,
                "tenant_id": str(domain_data["tenant_id"]),
                "m365": None,
                "cloudflare": None,
                "dkim": None,
                "mailboxes": None,
                "status": "checking",
            }
            summary["domains"].append(domain_result)

            try:
                logger.info(
                    "[%s] Objective reconciliation %s/%s",
                    domain_name,
                    idx,
                    len(domains),
                )
                m365 = await _verify_m365_domain(domain_data, auto_fix=auto_fix)
                domain_result["m365"] = {k: v for k, v in m365.items() if k != "access_token"}

                dkim, cf = ({"success": False, "enabled": False}, {"zone_ok": False})
                if m365.get("verified"):
                    dkim, cf = await _repair_dkim_if_needed(domain_data, auto_fix=auto_fix)
                else:
                    cf = await _ensure_cloudflare_truth(domain_data, None, auto_fix=auto_fix)
                domain_result["dkim"] = dkim
                domain_result["cloudflare"] = cf

                mailbox_truth = None
                expected_mailboxes: List[Dict[str, str]] = []
                if m365.get("verified") and dkim.get("enabled"):
                    display_name = " ".join(
                        part for part in [
                            domain_data.get("persona_first_name") or batch_data.get("persona_first_name") or "",
                            domain_data.get("persona_last_name") or batch_data.get("persona_last_name") or "",
                        ] if part
                    ).strip()
                    expected_mailboxes = build_expected_mailbox_data(
                        domain=domain_name,
                        display_name=display_name,
                        batch_data=batch_data,
                        mailboxes_per_tenant=batch_data["mailboxes_per_tenant"],
                    )
                    mailbox_truth = await _verify_mailbox_truth(domain_data, expected_mailboxes)

                    if auto_fix and not mailbox_truth.get("all_ok"):
                        await process_domain_fast(
                            domain_name=domain_name,
                            domain_id=domain_data["id"],
                            tenant_id=domain_data["tenant_id"],
                            admin_email=domain_data["tenant"]["admin_email"],
                            admin_password=domain_data["tenant"]["admin_password"],
                            display_name=display_name,
                            batch_id=batch_id,
                            batch_data=batch_data,
                            domain_index=domain_data["domain_index_in_tenant"],
                            mailboxes_per_tenant=batch_data["mailboxes_per_tenant"],
                            persona_first_name=domain_data.get("persona_first_name"),
                            persona_last_name=domain_data.get("persona_last_name"),
                        )
                        summary["repaired"] += 1
                        mailbox_truth = await _verify_mailbox_truth(domain_data, expected_mailboxes)

                    domain_result["mailboxes"] = mailbox_truth

                await _save_domain_truth(
                    domain_data=domain_data,
                    m365=m365,
                    dkim=dkim,
                    cf=cf,
                    mailbox=mailbox_truth,
                    expected_mailboxes=expected_mailboxes,
                )

                if m365.get("verified"):
                    summary["m365_verified"] += 1
                if dkim.get("enabled"):
                    summary["dkim_ok"] += 1
                if mailbox_truth and mailbox_truth.get("all_ok"):
                    summary["mailboxes_ok"] += 1

                domain_ok = bool(
                    m365.get("verified")
                    and dkim.get("enabled")
                    and mailbox_truth
                    and mailbox_truth.get("all_ok")
                )
                domain_result["status"] = "ok" if domain_ok else "incomplete"
                if not domain_ok:
                    summary["failed"] += 1

            except Exception as exc:
                logger.exception("[%s] Objective reconciliation failed", domain_name)
                domain_result["status"] = "error"
                domain_result["error"] = str(exc)
                summary["failed"] += 1
                summary["errors"].append(
                    {"domain": domain_name, "stage": "objective_domain", "error": str(exc)}
                )

            summary["domains_checked"] = idx
            await asyncio.sleep(1)

        if auto_fix and final_security_smtp:
            try:
                logger.info("Starting final SD/SMTP reconciliation for batch %s", batch_id)
                summary["security_smtp_reconciliation"] = await reconcile_batch(
                    batch_id,
                    auto_fix=True,
                )
            except Exception as exc:
                summary["errors"].append({"stage": "security_smtp_reconciliation", "error": str(exc)})

        summary["status"] = "completed"
        summary["completed_at"] = datetime.utcnow().isoformat()
        return summary

    except Exception as exc:
        logger.exception("Objective reconciliation for batch %s crashed", batch_id)
        summary["status"] = "error"
        summary["completed_at"] = datetime.utcnow().isoformat()
        summary["errors"].append({"stage": "objective_reconcile_batch", "error": str(exc)})
        return summary
