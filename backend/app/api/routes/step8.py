"""
Step 8 API Routes - Disable Security Defaults
==============================================

API endpoints to disable Security Defaults on M365 tenants.
This must be done BEFORE OAuth authentication with email sequencers
(PlusVibe, Smartlead, Instantly) will work.

Two paths are supported transparently:
  1. SD path  — the classic "Manage security defaults" Selenium flow.
  2. CA path  — for tenants where Microsoft hides SD behind Conditional
                 Access policies, we PATCH the Microsoft-managed MFA CA
                 policies via Graph instead.

Both paths converge on `security_defaults_disabled = true` so downstream
gating (mailbox automation, ROPC) can keep its single check.
"""

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.step8_security_defaults import (
    SecurityDefaultsDisabler,
    TenantCredentials,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/step8", tags=["Step 8 - Security Defaults"])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _persist_disable_result(
    db: AsyncSession,
    tenant_id: int,
    res: dict,
) -> None:
    """
    Write back the disable_for_tenant() result to the tenants table.

    Handles three cases:
      * SD path success                -> security_defaults_disabled = true
      * CA path success (reason='conditional_access_disabled')
                                       -> security_defaults_disabled = true
                                          + conditional_access_* columns
      * Failure                        -> security_defaults_error / ca_error
    """
    reason = res.get("reason")
    success = bool(res.get("success"))

    if success and reason == "conditional_access_disabled":
        await db.execute(
            text("""
                UPDATE tenants SET
                    security_defaults_disabled = true,
                    security_defaults_error = NULL,
                    security_defaults_disabled_at = COALESCE(security_defaults_disabled_at, NOW()),
                    conditional_access_disabled = true,
                    conditional_access_disabled_at = NOW(),
                    conditional_access_error = NULL,
                    conditional_access_policies_disabled_count = :count
                WHERE id = :tenant_id
            """),
            {
                "tenant_id": tenant_id,
                "count": int(
                    (res.get("ca_policies_disabled") or 0)
                    + (res.get("ca_policies_already_disabled") or 0)
                ),
            },
        )
        return

    if success:
        # Classic SD path success
        await db.execute(
            text("""
                UPDATE tenants SET
                    security_defaults_disabled = true,
                    security_defaults_error = NULL,
                    security_defaults_disabled_at = NOW()
                WHERE id = :tenant_id
            """),
            {"tenant_id": tenant_id},
        )
        return

    # Failure — record error in the appropriate column.
    err = res.get("error") or "Unknown error"
    if res.get("ca_total_policies") is not None or res.get("ca_token_method"):
        # CA path was attempted
        await db.execute(
            text("""
                UPDATE tenants SET
                    security_defaults_disabled = false,
                    security_defaults_error = :error,
                    conditional_access_error = :error
                WHERE id = :tenant_id
            """),
            {"tenant_id": tenant_id, "error": err[:1000]},
        )
    else:
        await db.execute(
            text("""
                UPDATE tenants SET
                    security_defaults_disabled = false,
                    security_defaults_error = :error
                WHERE id = :tenant_id
            """),
            {"tenant_id": tenant_id, "error": err[:1000]},
        )


def _summarize_result_for_response(res: dict) -> dict:
    """
    Build the user-facing JSON response from a disable_for_tenant() result.
    Always includes both SD and CA fields so the frontend can render badges
    without a second round-trip.
    """
    return {
        "success": bool(res.get("success")),
        "error": res.get("error"),
        "reason": res.get("reason"),
        "already_disabled": bool(res.get("already_disabled", False)),
        # CA path detail
        "ca_path_taken": res.get("reason") == "conditional_access_disabled"
        or res.get("ca_token_method") is not None,
        "ca_policies_disabled": res.get("ca_policies_disabled", 0),
        "ca_policies_already_disabled": res.get("ca_policies_already_disabled", 0),
        "ca_policies_disabled_names": res.get("ca_policies_disabled_names", []),
        "ca_policies_left_enabled": res.get("ca_policies_left_enabled", []),
        "ca_policies_failed": res.get("ca_policies_failed", []),
        "ca_total_policies": res.get("ca_total_policies", 0),
        "ca_targeted_count": res.get("ca_targeted_count", 0),
        "ca_token_method": res.get("ca_token_method"),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/disable/{tenant_id}")
async def disable_security_defaults_single(
    tenant_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db_session),
):
    """Disable Security Defaults (or CA-MFA policies) for a single tenant."""

    query = text("""
        SELECT
            t.id,
            t.admin_email,
            t.admin_password,
            t.totp_secret,
            d.name as domain
        FROM tenants t
        JOIN domains d ON t.domain_id = d.id
        WHERE t.id = :tenant_id
    """)
    result = await db.execute(query, {"tenant_id": tenant_id})
    row = result.fetchone()

    if not row:
        raise HTTPException(404, "Tenant not found")

    if not row.totp_secret:
        raise HTTPException(400, "Tenant missing TOTP secret - run Step 5 first")

    if not row.admin_password:
        raise HTTPException(400, "Tenant missing admin password")

    creds = TenantCredentials(
        tenant_id=str(row.id),
        domain=row.domain,
        admin_email=row.admin_email,
        admin_password=row.admin_password,
        totp_secret=row.totp_secret,
    )

    disabler = SecurityDefaultsDisabler(headless=True, worker_id=0)
    res = disabler.disable_for_tenant(creds)

    await _persist_disable_result(db, tenant_id, res)
    await db.commit()

    response = {"tenant_id": tenant_id, "domain": row.domain}
    response.update(_summarize_result_for_response(res))
    return response


@router.post("/disable-batch")
async def disable_security_defaults_batch(
    batch_size: int = 10,
    db: AsyncSession = Depends(get_db_session),
):
    """Disable Security Defaults (or CA-MFA policies) for a batch of tenants."""

    query = text("""
        SELECT
            t.id,
            t.admin_email,
            t.admin_password,
            t.totp_secret,
            d.name as domain
        FROM tenants t
        JOIN domains d ON t.domain_id = d.id
        WHERE t.security_defaults_disabled = false
        AND t.totp_secret IS NOT NULL
        AND t.admin_password IS NOT NULL
        ORDER BY t.created_at
        LIMIT :batch_size
    """)
    result = await db.execute(query, {"batch_size": batch_size})
    rows = result.fetchall()

    if not rows:
        return {"message": "No tenants need Security Defaults disabled", "processed": 0}

    tenants = [
        TenantCredentials(
            tenant_id=str(row.id),
            domain=row.domain,
            admin_email=row.admin_email,
            admin_password=row.admin_password,
            totp_secret=row.totp_secret,
        )
        for row in rows
    ]

    disabler = SecurityDefaultsDisabler(headless=True, worker_id=0)
    summary = disabler.disable_for_batch(tenants)

    # Persist each result and decorate it for the response
    decorated_results = []
    for res in summary["results"]:
        tid_str = res.get("tenant_id") or next(
            (t.tenant_id for t in tenants if t.domain == res["domain"]), None
        )
        if tid_str:
            try:
                tid_int = int(tid_str)
                await _persist_disable_result(db, tid_int, res)
            except Exception as e:
                logger.error("step8 batch persist failed for tenant %s: %s", tid_str, e)

        d = {"domain": res.get("domain"), "tenant_id": res.get("tenant_id")}
        d.update(_summarize_result_for_response(res))
        decorated_results.append(d)

    await db.commit()

    summary["results"] = decorated_results
    return summary


@router.get("/status")
async def get_security_defaults_status(db: AsyncSession = Depends(get_db_session)):
    """Get count of tenants by Security Defaults status (SD + CA breakdown)."""

    query = text("""
        SELECT
            COUNT(*) FILTER (WHERE security_defaults_disabled = true) as disabled,
            COUNT(*) FILTER (WHERE security_defaults_disabled = false AND totp_secret IS NOT NULL) as pending,
            COUNT(*) FILTER (WHERE totp_secret IS NULL) as not_ready,
            COUNT(*) FILTER (WHERE conditional_access_disabled = true) as ca_disabled,
            COUNT(*) FILTER (WHERE conditional_access_error IS NOT NULL) as ca_errored
        FROM tenants
    """)
    result = await db.execute(query)
    row = result.fetchone()

    return {
        "disabled": row.disabled or 0,
        "pending": row.pending or 0,
        "not_ready": row.not_ready or 0,
        "ca_disabled": row.ca_disabled or 0,
        "ca_errored": row.ca_errored or 0,
    }
