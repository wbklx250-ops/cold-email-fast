"""Release a removed domain's application-user licenses before its UPN is renamed."""

import asyncio
from urllib.parse import quote

import aiohttp

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"


def _select_users(users, domain_name, licensed_user_id=None, admin_email=None):
    """A stored object ID also identifies a user renamed by an earlier removal."""
    domain = domain_name.strip().lower()
    selected = []
    for user in users:
        upn = (user.get("userPrincipalName") or "").lower()
        if admin_email and upn == admin_email.lower():
            if user.get("id") == licensed_user_id or upn == f"me1@{domain}":
                raise ValueError("Refusing to release licenses from the tenant administrator")
            continue
        if upn == f"me1@{domain}":
            selected.append(user)
        elif licensed_user_id and user.get("id") == licensed_user_id:
            upn_domain = upn.rsplit("@", 1)[-1]
            if upn_domain != domain and not upn_domain.endswith(".onmicrosoft.com"):
                raise ValueError("Stored licensed user now belongs to another custom domain")
            selected.append(user)
    return selected


async def release_domain_user_licenses(access_token, domain_name, licensed_user_id=None, admin_email=None):
    """Fail closed on incomplete discovery, inherited licenses, or failed verification.

    Only the application's me1 user and the domain's recorded licensed user are
    eligible. Other domain users and tenant administrators are left untouched.
    Run before every removal tier so no fallback can bypass license cleanup.
    """
    result = {"success": False, "users": [], "licenses_removed": 0}
    try:
        async with aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as session:
            users = []
            url = (f"{GRAPH_ROOT}/users?$select=id,userPrincipalName,assignedLicenses,"
                   "licenseAssignmentStates&$top=999")
            while url:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"License discovery failed: HTTP {response.status}")
                    payload = await response.json()
                    users.extend(payload["value"])
                    url = payload.get("@odata.nextLink")

            targets = _select_users(users, domain_name, licensed_user_id, admin_email)
            # Check every target before making any change.
            for user in targets:
                if any(state.get("assignedByGroup") for state in user.get("licenseAssignmentStates", [])):
                    raise RuntimeError(
                        f"{user['userPrincipalName']} has group-assigned licenses; "
                        "release those assignments before removing the domain"
                    )
            for user in targets:
                sku_ids = [lic["skuId"] for lic in user["assignedLicenses"]]
                entry = {"user_id": user["id"], "upn": user["userPrincipalName"], "sku_ids": sku_ids}
                result["users"].append(entry)
                if not sku_ids:
                    entry["verified"] = True
                    continue
                user_url = f"{GRAPH_ROOT}/users/{quote(user['id'], safe='')}"
                async with session.post(
                    f"{user_url}/assignLicense",
                    json={"addLicenses": [], "removeLicenses": sku_ids},
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            f"License release failed for {entry['upn']}: HTTP {response.status}: "
                            f"{(await response.text())[:500]}"
                        )
                for attempt in range(6):
                    async with session.get(f"{user_url}?$select=id,assignedLicenses") as response:
                        if response.status != 200:
                            raise RuntimeError(f"License verification failed: HTTP {response.status}")
                        remaining = (await response.json())["assignedLicenses"]
                    if not remaining:
                        entry["verified"] = True
                        result["licenses_removed"] += len(sku_ids)
                        break
                    if attempt < 5:
                        await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"Licenses still assigned to {entry['upn']} after release")
        result["success"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result
