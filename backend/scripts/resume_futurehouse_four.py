"""Move the suspended tenant to a paused recovery batch; preserve the four ready domains.

Dry-run by default. The production pipeline can then resume at Step 8 with its
normal all-domain verification intact. No Microsoft or Cloudflare changes here.
"""
import argparse
import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['DEBUG'] = 'false'

from sqlalchemy import func, select
from app.db.session import async_session_factory
from app.models.batch import BatchStatus, SetupBatch
from app.models.domain import Domain
from app.models.mailbox import Mailbox
from app.models.tenant import Tenant
from app.models.pipeline_log import PipelineLog
from app.services.pipeline_readiness import first_blocker, m365_ready

BATCH = UUID('82a8ba8b-f0d0-47b6-a541-fa86fd2ba352')
HOLDING = uuid5(NAMESPACE_URL, f'{BATCH}/dunhill.investments/suspended-license')
BLOCKED = 'dunhill.investments'
READY = {'dunhill.finance', 'dunhill.financial', 'dunhill.ventures', 'familyofficeforum.co'}
CONFIG = (
    'redirect_url', 'persona_first_name', 'persona_last_name', 'mailboxes_per_tenant',
    'domains_per_tenant', 'sequencer_app_key', 'new_admin_password', 'sequencer_platform',
    'sequencer_login_email', 'sequencer_login_password', 'profile_photo_path', 'ns_confirmed_at',
)


async def main(apply):
    async with async_session_factory() as db:
        batch = (await db.execute(select(SetupBatch).where(SetupBatch.id == BATCH).with_for_update())).scalar_one()
        assert batch.pipeline_status != 'running', 'Batch is currently running'
        domains = list((await db.execute(select(Domain).where(Domain.batch_id == BATCH).with_for_update())).scalars())
        tenants = list((await db.execute(select(Tenant).where(Tenant.batch_id == BATCH).with_for_update())).scalars())
        existing = await db.get(SetupBatch, HOLDING)
        if existing:
            assert {d.name for d in domains} == READY
            print(json.dumps({'status': 'already_split', 'batch_id': str(BATCH), 'paused_batch_id': str(HOLDING)}))
            return
        assert {d.name for d in domains} == READY | {BLOCKED}, 'Batch membership changed'
        blocked = next(d for d in domains if d.name == BLOCKED)
        tenant = next(t for t in tenants if t.id == blocked.tenant_id)
        assert tenant.onmicrosoft_domain == 'orionstackhub1703.onmicrosoft.com'
        assert not blocked.step6_complete and blocked.step6_skipped
        assert sum(d.tenant_id == tenant.id for d in domains) == 1
        # Retired mailboxes from this tenant's previous domains may still exist;
        # only this batch's mailbox membership must be empty for the split.
        assert not (await db.scalar(select(func.count(Mailbox.id)).where(
            Mailbox.tenant_id == tenant.id, Mailbox.batch_id == BATCH,
        ))), 'Suspended tenant unexpectedly has mailboxes in the active batch'
        remaining = [d for d in domains if d.name in READY]
        remaining_tenants = [t for t in tenants if t.id != tenant.id]
        assert len(remaining_tenants) == 4
        blocker = first_blocker(remaining, remaining_tenants, before_step=8)
        assert blocker is None, str(blocker)
        counts = {}
        for d in remaining:
            count = await db.scalar(select(func.count(Mailbox.id)).where(
                Mailbox.tenant_id == d.tenant_id, Mailbox.email.ilike('%@' + d.name),
                Mailbox.created_in_exchange.is_(True), Mailbox.delegated.is_(True),
                Mailbox.password_set.is_(True), Mailbox.account_enabled.is_(True), Mailbox.upn_fixed.is_(True),
            ))
            assert count == batch.mailboxes_per_tenant, f'{d.name}: expected ready mailboxes missing'
            counts[d.name] = count
        summary = {'apply': apply, 'batch_id': str(BATCH), 'ready_mailboxes': counts,
                   'paused_batch_id': str(HOLDING), 'blocked_domain': BLOCKED}
        if not apply:
            print(json.dumps(summary))
            return
        now = datetime.now(timezone.utc)
        config = {key: copy.deepcopy(getattr(batch, key)) for key in CONFIG}
        holding = SetupBatch(
            id=HOLDING, name='Futurehouse - dunhill.investments (license suspended)',
            description=f'Paused license recovery split from batch {BATCH}. Reactivate the reseller subscription before retrying Step 7.',
            status=BatchStatus.PAUSED, pipeline_status='paused', pipeline_step=7,
            pipeline_step_name='Waiting for suspended Microsoft 365 subscription',
            pipeline_paused_at=now, current_step=7, auto_progress_enabled=False,
            total_domains=1, total_tenants=1, errors_count=1,
            zones_completed=int(bool(blocked.cloudflare_zone_id)),
            ns_propagated_count=int(bool(blocked.ns_propagated_at)),
            first_login_completed_count=int(bool(tenant.first_login_completed)),
            m365_completed=int(m365_ready(blocked)),
            **config,
        )
        if batch.custom_mailbox_map:
            holding.custom_mailbox_map = {k: v for k, v in batch.custom_mailbox_map.items() if k.lower() == BLOCKED}
            batch.custom_mailbox_map = {k: v for k, v in batch.custom_mailbox_map.items() if k.lower() != BLOCKED}
        db.add(holding)
        await db.flush()
        blocked.batch_id = HOLDING
        tenant.batch_id = HOLDING
        blocked.step6_skipped = False
        blocked.error_message = 'Paused: Microsoft 365 subscription suspended (0 enabled seats). Reseller reactivation required before Step 7.'
        batch.name = 'Futurehouse (4)'
        batch.total_domains = batch.total_tenants = 4
        batch.zones_completed = sum(bool(d.cloudflare_zone_id) for d in remaining)
        batch.ns_propagated_count = sum(bool(d.ns_propagated_at) for d in remaining)
        batch.dns_completed = sum(bool(d.dns_records_created) for d in remaining)
        batch.first_login_completed_count = sum(bool(t.first_login_completed) for t in remaining_tenants)
        batch.m365_completed = sum(m365_ready(d) for d in remaining)
        batch.mailboxes_completed_count = 4
        batch.smtp_completed = sum(bool(t.step7_smtp_auth_enabled) for t in remaining_tenants)
        batch.errors_count = 0
        batch.pipeline_status = 'paused'
        batch.status = BatchStatus.PAUSED
        batch.pipeline_paused_at = now
        batch.pipeline_step = 8
        batch.pipeline_step_name = 'Enable SMTP Auth'
        message = f'Continuing four verified domains (400 mailboxes). {BLOCKED} and its tenant moved to paused recovery batch {HOLDING} pending subscription reactivation.'
        for bid in (BATCH, HOLDING):
            db.add(PipelineLog(batch_id=bid, step=7, step_name='Create Mailboxes & Delegate', status='info', message=message))
        await db.commit()
        print(json.dumps({**summary, 'status': 'split_complete'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    asyncio.run(main(parser.parse_args().apply))
