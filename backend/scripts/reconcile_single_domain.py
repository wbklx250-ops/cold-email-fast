"""Objectively verify and repair mailbox setup for one domain."""

from __future__ import annotations

import argparse
import asyncio
import json
import os

os.environ["DEBUG"] = "false"

from app.services.objective_reconciliation import (  # noqa: E402
    _ensure_cloudflare_truth,
    _load_batch_domain_data,
    _repair_dkim_if_needed,
    _save_domain_truth,
    _verify_m365_domain,
    _verify_mailbox_truth,
)
from app.services.step7_fast import (  # noqa: E402
    build_expected_mailbox_data,
    process_domain_fast,
)


async def reconcile(domain_name: str, auto_fix: bool) -> dict:
    from sqlalchemy import select

    from app.db.session import async_session_factory
    from app.models.domain import Domain

    domain_name = domain_name.strip().lower()
    async with async_session_factory() as db:
        domain = (
            await db.execute(select(Domain).where(Domain.name == domain_name))
        ).scalar_one_or_none()
        if not domain or not domain.batch_id:
            return {"domain": domain_name, "status": "error", "error": "Domain or batch not found"}
        batch_id = domain.batch_id

    batch_data, domains = await _load_batch_domain_data(batch_id)
    domain_data = next((item for item in domains if item["name"] == domain_name), None)
    if not domain_data:
        return {"domain": domain_name, "status": "error", "error": "Linked domain data not found"}

    m365 = await _verify_m365_domain(domain_data, auto_fix=False)
    if not m365.get("verified"):
        return {
            "domain": domain_name,
            "status": "error",
            "error": m365.get("error") or "Domain is not verified in Microsoft 365",
        }

    dkim, cloudflare = await _repair_dkim_if_needed(domain_data, auto_fix=False)
    display_name = " ".join(
        part
        for part in (
            domain_data.get("persona_first_name") or batch_data.get("persona_first_name") or "",
            domain_data.get("persona_last_name") or batch_data.get("persona_last_name") or "",
        )
        if part
    ).strip()
    expected = build_expected_mailbox_data(
        domain=domain_name,
        display_name=display_name,
        batch_data=batch_data,
        mailboxes_per_tenant=batch_data["mailboxes_per_tenant"],
    )
    before = await _verify_mailbox_truth(domain_data, expected)

    repair_result = None
    if auto_fix and not before.get("all_ok"):
        repair_result = await process_domain_fast(
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

    after = await _verify_mailbox_truth(domain_data, expected)
    cloudflare = await _ensure_cloudflare_truth(domain_data, dkim, auto_fix=False)
    await _save_domain_truth(domain_data, m365, dkim, cloudflare, after, expected)

    def summary(truth: dict) -> dict:
        return {
            "all_ok": bool(truth.get("all_ok")),
            "expected": truth.get("expected"),
            "existing": truth.get("existing"),
            "account_enabled": truth.get("account_enabled"),
            "full_access": truth.get("full_access"),
            "send_as": truth.get("send_as"),
            "licensed_user_exists": truth.get("licensed_user_exists"),
            "licensed_user_has_allowed_license": truth.get("licensed_user_has_allowed_license"),
            "missing_count": len(truth.get("missing") or []),
            "disabled_count": len(truth.get("disabled_accounts") or []),
            "missing_full_access_count": len(truth.get("missing_full_access") or []),
            "missing_send_as_count": len(truth.get("missing_send_as") or []),
            "error": truth.get("error"),
        }

    return {
        "domain": domain_name,
        "status": "ok" if after.get("all_ok") else "incomplete",
        "before": summary(before),
        "repair_ran": repair_result is not None,
        "repair_success": repair_result.get("success") if repair_result else None,
        "repair_error": repair_result.get("error") if repair_result else None,
        "after": summary(after),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--domain", required=True)
    parser.add_argument("--auto-fix", action="store_true")
    args = parser.parse_args()
    print(json.dumps(await reconcile(args.domain, args.auto_fix), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
