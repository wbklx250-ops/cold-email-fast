"""
Conditional Access policy management via Microsoft Graph API.

PURPOSE
-------
Some M365 tenants ship with Microsoft-managed Conditional Access (CA) policies
enabled by default (the "MICROSOFT" badge ones in the Entra portal). When CA is
present, Microsoft hides the "Manage security defaults" link entirely and
replaces it with "Manage Conditional Access" — meaning our SD-disable Selenium
flow has nothing to click and our automation can't reach a "no MFA" state.

The fix: disable the Microsoft-managed MFA-enforcement CA policies via Graph
PATCH. After they're disabled, ROPC works, mailbox automation works, and the
existing reconciliation can verify state without Selenium.

SCOPE
-----
This module ONLY touches Microsoft-managed CA policies whose displayName looks
like an MFA enforcement policy. Custom tenant-defined CA policies are left
alone — they're surfaced in the result so the caller has visibility, but never
modified.

Pure httpx + stdlib. No Selenium. No PowerShell.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
CA_POLICIES_URL = f"{GRAPH_BASE}/identity/conditionalAccess/policies"

# Well-known Microsoft Azure PowerShell client ID — has Policy.ReadWrite.ConditionalAccess
# pre-consented as a first-party Microsoft app. Used by sd_graph.py too.
AZURE_PS_CLIENT_ID = "1b730954-1685-4b74-9bfd-dac224a7b894"

# Microsoft-managed MFA policy display names we explicitly target.
# These are what Microsoft auto-provisions on most modern tenants.
TARGETED_MFA_POLICY_NAMES = {
    "multifactor authentication for admins accessing microsoft admin portals",
    "multifactor authentication for admins",
    "multifactor authentication for all users",
    "multifactor authentication for azure management",
    "require multifactor authentication for admins",
    "require multifactor authentication for all users",
    "require multifactor authentication for azure management",
}


def _is_targeted_mfa_policy(policy: Dict) -> bool:
    """
    Decide whether a CA policy should be disabled by us.

    Targeting rule (intentionally conservative):
      1. Must be Microsoft-managed (templateId is non-empty), AND
      2. displayName matches one of the known MFA enforcement names
         (case-insensitive, trimmed), OR contains both "multifactor"
         and one of {"admin", "user", "azure"}.

    Custom tenant policies (templateId == None) are NEVER touched.
    """
    template_id = policy.get("templateId") or ""
    if not template_id:
        return False

    display = (policy.get("displayName") or "").strip().lower()
    if not display:
        return False

    if display in TARGETED_MFA_POLICY_NAMES:
        return True

    if "multifactor" in display and (
        "admin" in display or "user" in display or "azure" in display
    ):
        return True

    return False


async def list_ca_policies(access_token: str) -> Optional[List[Dict]]:
    """
    GET /identity/conditionalAccess/policies

    Returns the raw list of policies (each with id, displayName, state,
    templateId, conditions, grantControls...) or None on a hard failure.
    An empty list (tenant has no CA at all) is a valid success.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(CA_POLICIES_URL, headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                return payload.get("value", []) or []
            logger.warning(
                "CA list failed: %s %s", resp.status_code, resp.text[:300]
            )
            return None
    except Exception as e:
        logger.warning("CA list exception: %s", e)
        return None


async def disable_ca_policy(access_token: str, policy_id: str) -> Dict:
    """
    PATCH /identity/conditionalAccess/policies/{id}  body: {"state": "disabled"}

    Returns {"success": bool, "policy_id": str, "error": str|None}.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    url = f"{CA_POLICIES_URL}/{policy_id}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.patch(url, headers=headers, json={"state": "disabled"})
            if resp.status_code in (200, 204):
                return {"success": True, "policy_id": policy_id, "error": None}
            return {
                "success": False,
                "policy_id": policy_id,
                "error": f"PATCH {resp.status_code}: {resp.text[:300]}",
            }
    except Exception as e:
        return {"success": False, "policy_id": policy_id, "error": str(e)}


async def disable_targeted_mfa_policies(access_token: str) -> Dict:
    """
    Disable every Microsoft-managed MFA CA policy on the tenant.

    Returns:
        {
            "success": bool,                        # True iff every targeted policy is now disabled
            "total_policies": int,                  # total CA policies on tenant
            "targeted_count": int,                  # how many we identified as targets
            "disabled_now": int,                    # how many we changed state on this run
            "already_disabled": int,                # how many were already disabled
            "disabled_names": List[str],            # display names we acted on
            "left_enabled_non_targeted": List[Dict] # enabled but-not-touched policies for visibility
                # each: {id, displayName, templateId|None}
            "failed": List[Dict],                   # per-policy failures
            "errors": List[str],                    # high-level error strings
        }
    """
    result: Dict = {
        "success": False,
        "total_policies": 0,
        "targeted_count": 0,
        "disabled_now": 0,
        "already_disabled": 0,
        "disabled_names": [],
        "left_enabled_non_targeted": [],
        "failed": [],
        "errors": [],
    }

    policies = await list_ca_policies(access_token)
    if policies is None:
        result["errors"].append("Could not list CA policies via Graph")
        return result

    result["total_policies"] = len(policies)

    if not policies:
        # No CA at all on this tenant — vacuously success
        result["success"] = True
        return result

    for p in policies:
        is_targeted = _is_targeted_mfa_policy(p)
        state = (p.get("state") or "").lower()
        name = p.get("displayName") or "<unnamed>"
        pid = p.get("id")

        if not is_targeted:
            # Track non-targeted enabled policies for visibility but never modify
            if state == "enabled":
                result["left_enabled_non_targeted"].append(
                    {
                        "id": pid,
                        "displayName": name,
                        "templateId": p.get("templateId"),
                    }
                )
            continue

        result["targeted_count"] += 1

        if state == "disabled":
            result["already_disabled"] += 1
            continue

        # Targeted and enabled (or reporting-only) → disable
        patch_res = await disable_ca_policy(access_token, pid)
        if patch_res["success"]:
            result["disabled_now"] += 1
            result["disabled_names"].append(name)
        else:
            result["failed"].append(
                {
                    "id": pid,
                    "displayName": name,
                    "error": patch_res["error"],
                }
            )

    # Verify
    verified = await verify_targeted_mfa_disabled(access_token)
    result["success"] = verified and not result["failed"]
    return result


async def verify_targeted_mfa_disabled(access_token: str) -> bool:
    """
    Re-list policies and assert every targeted MFA policy is now state==disabled.

    Returns True if all good. False on read failure or any leftover enabled.
    """
    policies = await list_ca_policies(access_token)
    if policies is None:
        return False

    for p in policies:
        if not _is_targeted_mfa_policy(p):
            continue
        if (p.get("state") or "").lower() != "disabled":
            return False

    return True
