# Bulk domain swaps

Open **Domain Swaps** in the sidebar (`/domain-swaps`). Paste current domains or tenant identifiers into the first list and replacement domains into the second, one entry per line. Lines are paired in order. Preview the mappings, review the tenant, old domain, replacement and mailbox count, then confirm and start.

Tenant identifiers can be the database UUID, Microsoft tenant ID, tenant name, admin email or onmicrosoft domain. When a tenant has multiple domains, specify each current domain being replaced. Multiple replacements for one tenant share one new setup batch. Other completed domains on that tenant remain linked.

The preview checks database ownership, duplicate mappings, active automation, mailbox configuration and conflicting swaps. Start revalidates the saved preview before reserving resources. Before each removal, the worker screens the replacement through the existing Microsoft domain lookup and requires Cloudflare configuration. The normal authenticated provisioning process remains responsible for Microsoft verification and DNS readiness.

## Execution and recovery

1. Release the old domain user's license using the existing removal service and its stable user ID.
2. Remove the Microsoft domain and its dependencies, then clean its Microsoft email DNS records. All three operations must report success before database unlinking and replacement setup.
3. Retire the old domain and suspend its mailbox records. Their records remain available for history. This does not copy mailbox contents to the replacement.
4. Save the removal checkpoint in the same database commit as retirement. Create one replacement setup batch per affected tenant, retaining the same tenant and admin credentials.
5. Run the existing pipeline for Cloudflare, nameservers, Microsoft verification, DKIM, licensed users, mailboxes, SMTP and reconciliation. Existing mailbox local parts and display names are transferred to the replacement configuration; normal provisioning passwords apply. If mailbox records are absent, use the existing custom mailbox map or persona and count.

Use **Open setup / nameservers** to see detailed progress and update registrar nameservers when needed. Use **Retry / resume unfinished swaps** after resolving an error. Successful removals and created batches are reused. Failed tenant groups do not prevent the remaining tenant groups from being attempted. If several domains on one tenant are selected, all their cleanups must finish before that tenant's replacement setup starts.

Jobs and mappings live in PostgreSQL, not browser memory. The worker starts with the backend, checks for work every ten seconds, and serializes swaps with a transaction-scoped PostgreSQL advisory lock. A restart resumes queued/interrupted swap jobs from saved checkpoints. A failure between a Microsoft operation and the database commit retries the existing idempotent removal flow. Preview-only jobs never run automatically. Completed jobs release their reservations. Failed jobs keep them while awaiting recovery.

Old batches containing a reserved domain/tenant cannot resume their pipeline during the swap. The standalone database removal flow also refuses reserved domains. Other administration tools should not be used to manually relink or delete resources belonging to an unfinished swap.

The feature provisions replacements; it does not purchase domains, migrate old messages or upload replacement mailboxes to a sequencer. Redirects, personas and the sequencer app selection are inherited. Use the existing sequencer upload flow once setup is complete.

## API

- `POST /api/v1/domain-swaps/preview`: `{ "name": "September replacements", "sources": ["old.example"], "replacements": ["new.example"] }`
- `POST /api/v1/domain-swaps/{job_id}/start`: run the saved reviewed plan; repeated starts are idempotent.
- `GET /api/v1/domain-swaps/{job_id}`: mappings, cleanup checkpoints and current pipeline progress.
- `GET /api/v1/domain-swaps`: recent started jobs.
- `POST /api/v1/domain-swaps/{job_id}/retry`: queue unfinished work; running/completed jobs are not duplicated.

## Installation and verification

Deploy backend and frontend from the same checkout. Migration `031_domain_swaps` adds the job and reservation tables; the existing backend startup script runs `alembic upgrade head`. For a manual installation, run that command from `backend/` against the intended application database before starting the backend.

Focused backend checks:

```text
python -m pytest tests/test_domain_swaps.py tests/test_domain_license_cleanup.py tests/test_pipeline_readiness.py tests/test_step7_fast_runtime.py -q
```

Frontend: `npm ci` then `npm run build` in `frontend/`. Tests use an isolated SQLite database and mocked Microsoft/Cloudflare/pipeline calls; they do not operate on live tenants. The PostgreSQL migration can also be inspected with `alembic upgrade 030_domain_check_success:031_domain_swaps --sql`.
