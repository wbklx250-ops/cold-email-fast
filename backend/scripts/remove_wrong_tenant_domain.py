from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["DEBUG"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.tenant import Tenant
from app.services.selenium.domain_removal import remove_domain_robust


async def main(domain: str, source: str) -> int:
    async with async_session_factory() as db:
        tenant = (
            await db.execute(select(Tenant).where(Tenant.onmicrosoft_domain == source))
        ).scalar_one_or_none()
    if not tenant or not tenant.admin_email or not tenant.admin_password:
        print(json.dumps({"success": False, "error": "source tenant credentials unavailable"}))
        return 2
    result = await asyncio.to_thread(
        remove_domain_robust,
        domain,
        tenant.admin_email,
        tenant.admin_password,
        tenant.totp_secret,
        True,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("domain")
    parser.add_argument("source_onmicrosoft_domain")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.domain, args.source_onmicrosoft_domain)))
