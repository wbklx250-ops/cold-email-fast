import csv
import io

import pytest
from fastapi import HTTPException

from app.api.routes.mailboxes import (
    DomainMailboxExportRequest,
    _parse_export_domains,
    export_mailbox_credentials_by_domains,
)
from app.models.mailbox import Mailbox, MailboxStatus, WarmupStage
from app.models.tenant import Tenant, TenantStatus


async def _streaming_response_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks).decode()


def test_parse_export_domains_accepts_batches_and_email_values():
    domains, invalid = _parse_export_domains(
        "https://Example.com/path, user@OtherDomain.co.nz\n*.third-domain.io"
    )

    assert domains == ["example.com", "otherdomain.co.nz", "third-domain.io"]
    assert invalid == []


def test_parse_export_domains_reports_invalid_values():
    domains, invalid = _parse_export_domains(["valid.com", "not_a_domain"])

    assert domains == ["valid.com"]
    assert invalid == ["not_a_domain"]


@pytest.mark.asyncio
async def test_export_mailbox_credentials_by_domains_returns_upload_csv(test_session):
    tenant = Tenant(
        microsoft_tenant_id="11111111-1111-1111-1111-111111111111",
        name="Example Tenant",
        onmicrosoft_domain="example.onmicrosoft.com",
        provider="Provider",
        admin_email="admin@example.onmicrosoft.com",
        admin_password="Password123!",
        custom_domain="example.com",
        status=TenantStatus.NEW,
    )
    test_session.add(tenant)
    await test_session.flush()

    test_session.add_all(
        [
            Mailbox(
                email="zara@example.com",
                display_name="Zara Example",
                password=None,
                initial_password="Initial123!",
                tenant_id=tenant.id,
                status=MailboxStatus.READY,
                warmup_stage=WarmupStage.NONE,
            ),
            Mailbox(
                email="adam@example.com",
                display_name="Adam Example",
                password="Mailbox123!",
                tenant_id=tenant.id,
                status=MailboxStatus.PENDING,
                warmup_stage=WarmupStage.NONE,
            ),
            Mailbox(
                email="ignored@otherdomain.com",
                display_name="Ignored Domain",
                password="Other123!",
                tenant_id=tenant.id,
                status=MailboxStatus.READY,
                warmup_stage=WarmupStage.NONE,
            ),
        ]
    )
    await test_session.commit()

    response = await export_mailbox_credentials_by_domains(
        DomainMailboxExportRequest(domains=["EXAMPLE.com", "missing.com"]),
        db=test_session,
    )
    csv_text = await _streaming_response_text(response)
    rows = list(csv.reader(io.StringIO(csv_text)))

    assert rows == [
        ["DisplayName", "EmailAddress", "Password", "Domain", "TenantName"],
        ["Adam Example", "adam@example.com", "Mailbox123!", "example.com", "Example Tenant"],
        ["Zara Example", "zara@example.com", "Initial123!", "example.com", "Example Tenant"],
    ]
    assert response.headers["x-mailbox-count"] == "2"
    assert response.headers["x-domain-count"] == "2"
    assert response.headers["x-missing-domains"] == "missing.com"


@pytest.mark.asyncio
async def test_export_mailbox_credentials_by_domains_404s_when_no_matches(test_session):
    with pytest.raises(HTTPException) as exc_info:
        await export_mailbox_credentials_by_domains(
            DomainMailboxExportRequest(domains=["missing.com"]),
            db=test_session,
        )

    assert exc_info.value.status_code == 404
