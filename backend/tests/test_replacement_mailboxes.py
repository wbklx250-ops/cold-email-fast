from uuid import uuid4

import pytest
from sqlalchemy import select, func

from app.models.batch import SetupBatch
from app.models.domain import Domain, DomainStatus
from app.models.mailbox import Mailbox, MailboxStatus
from app.models.tenant import Tenant, TenantStatus
from app.services.step7_fast import _prepare_expected_mailboxes


def tenant(name):
    return Tenant(name=name, microsoft_tenant_id=str(uuid4()), provider="test",
        onmicrosoft_domain=f"{name}.onmicrosoft.com", admin_email=f"admin@{name}.onmicrosoft.com",
        admin_password="test", status=TenantStatus.NEW)


@pytest.mark.asyncio
async def test_replacement_reuses_unique_address_and_clears_old_evidence(test_session):
    db = test_session
    old, new = tenant("old"), tenant("new")
    batch = SetupBatch(name="replacement")
    db.add_all([old, new, batch])
    await db.flush()
    new.batch_id = batch.id
    domain = Domain(name="example.com", tld="com", status=DomainStatus.ACTIVE,
        cloudflare_zone_status="active", tenant_id=new.id, batch_id=batch.id,
        domain_verified_in_m365=True)
    row = Mailbox(email="bill@example.com", display_name="Previous", tenant_id=old.id,
        status=MailboxStatus.READY, warmup_stage="none", setup_complete=True,
        created_in_exchange=True, delegated=True, password_set=True,
        account_enabled=True, upn_fixed=True, microsoft_object_id="old-object-id",
        uploaded_to_sequencer=True)
    unrelated = Mailbox(email="bill@unrelated.com", display_name="Other", tenant_id=old.id,
        status=MailboxStatus.READY, warmup_stage="none", setup_complete=True)
    db.add_all([domain, row, unrelated])
    await db.commit()
    original_id = row.id
    expected = [dict(email="bill@example.com", local_part="bill", display_name="Bill New", password="test-new")]
    assert await _prepare_expected_mailboxes(db, domain.id, new.id, batch.id, expected) == 0
    assert row.id == original_id
    assert row.tenant_id == new.id
    assert row.batch_id == batch.id
    assert row.display_name == "Bill New"
    assert row.microsoft_object_id is None
    assert not row.uploaded_to_sequencer
    for field in ("setup_complete", "created_in_exchange", "delegated", "password_set", "account_enabled", "upn_fixed"):
        assert getattr(row, field) is False
    assert unrelated.tenant_id == old.id
    assert unrelated.setup_complete
    assert await _prepare_expected_mailboxes(db, domain.id, new.id, batch.id, expected) == 0
    assert await db.scalar(select(func.count(Mailbox.id))) == 2


@pytest.mark.asyncio
async def test_mailbox_reassignment_requires_verified_destination(test_session):
    db = test_session
    target = tenant("target")
    batch = SetupBatch(name="unverified")
    db.add_all([target, batch])
    await db.flush()
    domain = Domain(name="example.com", tld="com", status=DomainStatus.PURCHASED,
        cloudflare_zone_status="pending", tenant_id=target.id, batch_id=batch.id,
        domain_verified_in_m365=False)
    db.add(domain)
    await db.commit()
    expected = [dict(email="bill@example.com", local_part="bill", display_name="Bill", password="test")]
    with pytest.raises(RuntimeError, match="verified tenant"):
        await _prepare_expected_mailboxes(db, domain.id, target.id, batch.id, expected)
    assert await db.scalar(select(func.count(Mailbox.id))) == 0
    domain.domain_verified_in_m365 = True
    await db.commit()
    with pytest.raises(RuntimeError, match="verified domain"):
        await _prepare_expected_mailboxes(db, domain.id, target.id, batch.id, [dict(expected[0], email="bill@other.com")])
