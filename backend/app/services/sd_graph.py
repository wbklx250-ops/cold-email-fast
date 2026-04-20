"""
Security Defaults management via Microsoft Graph API.
Used for verification and repair AFTER the initial Selenium-based disable
(which is needed to break the SD-blocks-auth chicken-and-egg).

PURE httpx + stdlib. NO Selenium. NO Chrome.
"""

import asyncio
import logging
from typing import Dict, Optional
import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SD_POLICY_URL = f"{GRAPH_BASE}/policies/identitySecurityDefaultsEnforcementPolicy"

# Well-known Microsoft Azure PowerShell client ID — has
# Policy.ReadWrite.ConditionalAccess as a first-party consent so ROPC flows
# do not need admin consent on each tenant.
AZURE_PS_CLIENT_ID = "1b730954-1685-4b74-9bfd-dac224a7b894"


async def get_ropc_graph_token(tenant_domain: str, admin_email: str, admin_password: str) -> Optional[Dict]:
    """
    Mint a Graph token via ROPC. REQUIRES Security Defaults already disabled on the tenant
    (otherwise Microsoft returns AADSTS50076/AADSTS50079 forcing interactive MFA).

    Returns {"access_token": str, "expires_in": int} or None on failure.

    tenant_domain: e.g. "contoso.onmicrosoft.com" — used in the token URL path.
    """
    token_url = f"https://login.microsoftonline.com/{tenant_domain}/oauth2/v2.0/token"
    data = {
        "grant_type": "password",
        "client_id": AZURE_PS_CLIENT_ID,
        "scope": "https://graph.microsoft.com/.default",
        "username": admin_email,
        "password": admin_password,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(token_url, data=data)
            if resp.status_code == 200:
                j = resp.json()
                return {"access_token": j["access_token"], "expires_in": j.get("expires_in", 3600)}
            err = {}
            try:
                err = resp.json() if resp.content else {}
            except Exception:
                pass
            logger.warning(
                "ROPC token failed for %s: %s %s",
                admin_email,
                resp.status_code,
                (err.get("error_description", "") or "")[:200],
            )
            return None
    except Exception as e:
        logger.warning("ROPC token exception for %s: %s", admin_email, e)
        return None


async def read_sd_state(access_token: str) -> Optional[bool]:
    """Returns True if SD is ENABLED, False if DISABLED, None if unreadable."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(SD_POLICY_URL, headers={"Authorization": f"Bearer {access_token}"})
            if resp.status_code == 200:
                return bool(resp.json().get("isEnabled"))
            logger.warning("Graph SD read failed: %s %s", resp.status_code, resp.text[:200])
            return None
    except Exception as e:
        logger.warning("Graph SD read exception: %s", e)
        return None


async def disable_sd_via_graph(access_token: str) -> Dict:
    """
    PATCH SD policy to isEnabled=false. Reads back to verify.
    Returns {success, before, after, error}.
    """
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    result: Dict = {"success": False, "before": None, "after": None, "error": None}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            before_resp = await client.get(SD_POLICY_URL, headers=headers)
            if before_resp.status_code == 200:
                result["before"] = bool(before_resp.json().get("isEnabled"))

            if result["before"] is False:
                # Already disabled — no-op
                result["success"] = True
                result["after"] = False
                return result

            patch_resp = await client.patch(SD_POLICY_URL, headers=headers, json={"isEnabled": False})
            if patch_resp.status_code not in (200, 204):
                result["error"] = f"PATCH returned {patch_resp.status_code}: {patch_resp.text[:300]}"
                return result

            await asyncio.sleep(2)
            after_resp = await client.get(SD_POLICY_URL, headers=headers)
            if after_resp.status_code == 200:
                result["after"] = bool(after_resp.json().get("isEnabled"))
                result["success"] = (result["after"] is False)
            else:
                result["error"] = f"Verification GET failed: {after_resp.status_code}"
            return result
    except Exception as e:
        result["error"] = str(e)
        return result


async def verify_or_repair_sd(
    tenant_domain: str,
    admin_email: str,
    admin_password: str,
    auto_fix: bool = True,
) -> Dict:
    """
    End-to-end: mint ROPC token, read SD state, optionally repair.

    Returns {success, sd_disabled, action, error}.
    action ∈ {'verified_ok', 'repaired', 'already_disabled',
              'token_failed', 'drift_detected', 'unfixable'}
    """
    out: Dict = {"success": False, "sd_disabled": None, "action": None, "error": None}

    token = await get_ropc_graph_token(tenant_domain, admin_email, admin_password)
    if not token:
        out["action"] = "token_failed"
        out["error"] = (
            "Could not mint Graph token via ROPC — SD may still be enabled, "
            "or credentials invalid"
        )
        return out

    state = await read_sd_state(token["access_token"])
    if state is None:
        out["action"] = "unfixable"
        out["error"] = "Could not read SD state via Graph"
        return out

    if state is False:
        out["success"] = True
        out["sd_disabled"] = True
        out["action"] = "already_disabled"
        return out

    # SD is enabled — drift detected
    if not auto_fix:
        out["sd_disabled"] = False
        out["action"] = "drift_detected"
        out["error"] = "SD is enabled, auto_fix=False"
        return out

    fix = await disable_sd_via_graph(token["access_token"])
    out["success"] = fix["success"]
    out["sd_disabled"] = (fix["after"] is False)
    out["action"] = "repaired" if fix["success"] else "unfixable"
    out["error"] = fix.get("error")
    return out
