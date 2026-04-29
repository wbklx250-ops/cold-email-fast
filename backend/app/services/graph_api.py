"""
Microsoft Graph API Service

Direct API calls for user operations - no Selenium UI needed.
"""

import aiohttp
import logging
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)


class GraphAPIService:
    """Microsoft Graph API service for user operations."""

    BASE_URL = "https://graph.microsoft.com/v1.0"

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make Graph API request."""
        url = f"{self.BASE_URL}{endpoint}"

        async with aiohttp.ClientSession() as session:
            async with session.request(
                method=method,
                url=url,
                headers=self.headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                response_text = await response.text()

                if response.status >= 400:
                    logger.error("Graph API error %s: %s", response.status, response_text)
                    return {"error": response_text, "status": response.status}

                if response_text:
                    try:
                        return await response.json()
                    except Exception:
                        return {"text": response_text}

                return {"success": True}

    # =========================================================================
    # USER OPERATIONS
    # =========================================================================

    async def create_user(
        self,
        display_name: str,
        user_principal_name: str,
        password: str,
        mail_nickname: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a new user.

        Args:
            display_name: Display name (e.g., "Licensed User")
            user_principal_name: UPN (e.g., "me1@tenant.onmicrosoft.com")
            password: User password
            mail_nickname: Mail nickname (defaults to username part)
        """
        if mail_nickname is None:
            mail_nickname = user_principal_name.split("@")[0]

        payload = {
            "accountEnabled": True,
            "displayName": display_name,
            "mailNickname": mail_nickname,
            "userPrincipalName": user_principal_name,
            "passwordProfile": {
                "forceChangePasswordNextSignIn": False,
                "password": password,
            },
        }

        result = await self._request("POST", "/users", payload)

        if "error" in result:
            # Check if user already exists
            if "already exists" in str(result.get("error", "")).lower():
                logger.info("User %s already exists", user_principal_name)
                existing = await self.get_user(user_principal_name)
                return {"success": True, "user": existing, "already_existed": True}
            return {"success": False, "error": result["error"]}

        logger.info("Created user: %s", user_principal_name)
        return {"success": True, "user": result}

    async def get_user(self, user_principal_name: str) -> Optional[Dict]:
        """Get user by UPN."""
        result = await self._request("GET", f"/users/{user_principal_name}")
        if "error" in result:
            return None
        return result

    async def get_available_licenses(self) -> List[Dict]:
        """Get available license SKUs."""
        result = await self._request("GET", "/subscribedSkus")
        if "error" in result:
            return []
        return result.get("value", [])

    async def assign_license(self, user_id: str, sku_id: str) -> Dict[str, Any]:
        """Assign license to user."""
        payload = {
            "addLicenses": [{"skuId": sku_id}],
            "removeLicenses": [],
        }

        result = await self._request("POST", f"/users/{user_id}/assignLicense", payload)

        if "error" in result:
            return {"success": False, "error": result["error"]}

        logger.info("Assigned license %s to user %s", sku_id, user_id)
        return {"success": True}

    async def create_licensed_user(
        self,
        onmicrosoft_domain: str,
        display_name: str = "Licensed User",
        password: str = "#Sendemails1",
    ) -> Dict[str, Any]:
        """
        Create licensed user (me1) - complete flow.

        1. Create user
        2. Get available license
        3. Assign license
        """
        upn = f"me1@{onmicrosoft_domain}"

        # Step 1: Create user
        create_result = await self.create_user(
            display_name=display_name,
            user_principal_name=upn,
            password=password,
        )

        if not create_result.get("success"):
            return create_result

        user = create_result.get("user", {})
        user_id = user.get("id")

        if not user_id:
            return {"success": False, "error": "No user ID returned"}

        # Step 2: Get available license — ONLY use Microsoft 365 Business Basic
        # (O365_BUSINESS_ESSENTIALS) or Exchange Online Plan 1 (EXCHANGESTANDARD).
        # Tenants from our provider may include trial SKUs and unrelated paid
        # subs (E3/E5, Defender, Teams Essentials, etc.) — we must skip those.
        # Prefer Business Basic over Exchange Online Plan 1 when both have a
        # free seat available.
        ALLOWED_SKUS = ("O365_BUSINESS_ESSENTIALS", "EXCHANGESTANDARD")
        SKU_PREFERENCE = {"O365_BUSINESS_ESSENTIALS": 0, "EXCHANGESTANDARD": 1}

        licenses = await self.get_available_licenses()
        candidates = []
        for lic in licenses:
            sku_part = (lic.get("skuPartNumber") or "").upper()
            if sku_part not in ALLOWED_SKUS:
                continue
            # Hard-skip any trial SKU even if the part number happened to match.
            if "TRIAL" in sku_part:
                continue
            # Must apply to users (not e.g. service-plan-only) and have a seat.
            if lic.get("appliesTo") and lic.get("appliesTo") != "User":
                continue
            consumed = lic.get("consumedUnits", 0)
            enabled = lic.get("prepaidUnits", {}).get("enabled", 0)
            if enabled <= 0 or enabled <= consumed:
                continue
            candidates.append((SKU_PREFERENCE.get(sku_part, 99), lic))

        candidates.sort(key=lambda x: x[0])
        available_license = candidates[0][1].get("skuId") if candidates else None

        if not available_license:
            return {
                "success": False,
                "email": upn,
                "password": password,
                "user_id": user_id,
                "license_assigned": False,
                "error": (
                    "No available 'Microsoft 365 Business Basic' "
                    "(O365_BUSINESS_ESSENTIALS) or 'Exchange Online Plan 1' "
                    "(EXCHANGESTANDARD) license with a free seat in this tenant"
                ),
            }

        # Step 3: Assign license
        license_result = await self.assign_license(user_id, available_license)

        return {
            "success": True,
            "email": upn,
            "password": password,
            "user_id": user_id,
            "license_assigned": license_result.get("success", False),
        }

    async def enable_user(self, user_principal_name: str) -> Dict[str, Any]:
        """Enable a user account."""
        payload = {"accountEnabled": True}
        result = await self._request("PATCH", f"/users/{user_principal_name}", payload)

        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True}

    async def set_password(self, user_principal_name: str, password: str) -> Dict[str, Any]:
        """Set user password."""
        payload = {
            "passwordProfile": {
                "forceChangePasswordNextSignIn": False,
                "password": password,
            }
        }
        result = await self._request("PATCH", f"/users/{user_principal_name}", payload)

        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True}

    async def update_upn(self, current_upn: str, new_upn: str) -> Dict[str, Any]:
        """Update user principal name."""
        payload = {"userPrincipalName": new_upn}
        result = await self._request("PATCH", f"/users/{current_upn}", payload)

        if "error" in result:
            return {"success": False, "error": result["error"]}
        return {"success": True}

    # =========================================================================
    # BULK OPERATIONS
    # =========================================================================

    async def enable_users_bulk(self, upns: List[str]) -> Dict[str, Any]:
        """Enable multiple users."""
        results = {"enabled": [], "failed": []}
        for upn in upns:
            result = await self.enable_user(upn)
            if result.get("success"):
                results["enabled"].append(upn)
            else:
                results["failed"].append({"upn": upn, "error": result.get("error")})
        return results

    async def set_passwords_bulk(self, users: List[Dict[str, str]]) -> Dict[str, Any]:
        """Set passwords for multiple users. users = [{"upn": ..., "password": ...}]"""
        results = {"set": [], "failed": []}
        for user in users:
            result = await self.set_password(user["upn"], user["password"])
            if result.get("success"):
                results["set"].append(user["upn"])
            else:
                results["failed"].append({"upn": user["upn"], "error": result.get("error")})
        return results

    async def update_upns_bulk(self, users: List[Dict[str, str]]) -> Dict[str, Any]:
        """Update UPNs for multiple users. users = [{"current_upn": ..., "new_upn": ...}]"""
        results = {"updated": [], "failed": []}
        for user in users:
            result = await self.update_upn(user["current_upn"], user["new_upn"])
            if result.get("success"):
                results["updated"].append(user["new_upn"])
            else:
                results["failed"].append(
                    {"upn": user["current_upn"], "error": result.get("error")}
                )
        return results