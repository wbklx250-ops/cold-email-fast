from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["DEBUG"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select

from app.db.session import async_session_factory
from app.models.tenant import Tenant


SOURCES = ("orionstackstack1705", "ultranetsync1849", "orionstackbase1700", "optinexzone1693")


async def main() -> None:
    async with async_session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Tenant).where(
                        or_(
                            Tenant.onmicrosoft_domain.ilike(f"%{SOURCES[0]}%"),
                            Tenant.onmicrosoft_domain.ilike(f"%{SOURCES[1]}%"),
                            Tenant.onmicrosoft_domain.ilike(f"%{SOURCES[2]}%"),
                            Tenant.onmicrosoft_domain.ilike(f"%{SOURCES[3]}%"),
                            Tenant.name.ilike(f"%{SOURCES[0]}%"),
                            Tenant.name.ilike(f"%{SOURCES[1]}%"),
                            Tenant.name.ilike(f"%{SOURCES[2]}%"),
                            Tenant.name.ilike(f"%{SOURCES[3]}%"),
                        )
                    )
                )
            ).scalars().all()
        )
    print(json.dumps([
        {
            "id": str(row.id),
            "name": row.name,
            "onmicrosoft_domain": row.onmicrosoft_domain,
            "microsoft_tenant_id": str(row.microsoft_tenant_id) if row.microsoft_tenant_id else None,
            "admin_email": row.admin_email,
            "has_password": bool(row.admin_password),
            "has_totp": bool(row.totp_secret),
        }
        for row in rows
    ], indent=2))


if __name__ == "__main__":
    asyncio.run(main())
