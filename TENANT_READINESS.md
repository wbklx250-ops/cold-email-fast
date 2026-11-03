# Tenant readiness

The app's **Tenant Readiness** page (`/tools/tenant-readiness`) starts checks from a CSV,
shows saved results, and hands off an existing Microsoft sign-in or missing payment method
through a protected live browser. Closing the page or laptop does not stop a queued check.

The backend starts a database-backed queue with four browser workers by default. Up to forty
NDR monitors run separately, releasing browser capacity as soon as the message is sent. Configure
`READINESS_PARALLEL_BROWSERS` to change browser concurrency (maximum twelve). The queue also
caps concurrency against the container process/thread limit and reserves capacity for the API.
The existing 8 GB Railway service has a tighter process limit than memory limit.

`READINESS_OPERATOR_KEY` protects the backend readiness endpoints. The frontend proxy adds
this server-only service key; the page follows the app's existing access flow without a separate
readiness login. No credential or service key is bundled into client JavaScript. The frontend
proxies only this API path to the existing backend. Credentials, TOTP seeds and MSAL caches
use the app's configured encryption key.

Rows in `readiness_checks` hold the queue and checkpoints. A worker claims rows with
`FOR UPDATE SKIP LOCKED` and a two-minute lease, renewed every five seconds. Expired work
can resume after a server restart. The send timestamp and unique subject commit before
`sendMail`; uncertain sends are recovered from Sent Items and never automatically repeated.
The final trial order is also checkpointed before clicking. An uncertain order requires
review instead of another purchase attempt. Explicitly skipped and completed tenants remain terminal.

The initial CSV may contain `TargetTenantID`, `Tenant Number`, `AdminUsername` and
`AdminPassword`. Extra columns are data and are ignored. The recipient is fixed to the
authorized test inbox, `wbklx250@gmail.com`. A clean result requires successful mailbox scans
for five minutes after an accepted or recovered send; it does not independently verify receipt.
Microsoft provisioning, trial eligibility, existing MFA and billing requirements still apply.
Trials must show zero due today; the shortest offered term is selected and renewal stays unchanged.

Microsoft sometimes requires a separate recurring-payment authorization checkbox before its final
trial order button is enabled. The operator explicitly authorized accepting that required consent
for these tenant trials on 2026-11-04. `READINESS_ACCEPT_TRIAL_RECURRING_PAYMENTS=true` and
`READINESS_TRIAL_CONSENT_TENANT_IDS` (a JSON list of the 34 supplied tenant GUIDs) enable
that behavior for this batch; new tenants are not automatically included. The code default is
false. Only the specifically labeled payment-consent checkbox
is accepted, after verifying the free checkout and selecting the shortest term. Other renewal
controls retain their defaults. Unconfirmed final orders remain protected from automatic repetition.

The local `tenant-readiness/migrate_to_cloud.py` transfers existing results and DPAPI-protected
authentication checkpoints directly over TLS after the local runner has stopped. It never writes
plaintext credential exports. Do not run the local runner again against a tenant being checked
by the server.

Validation: `python -m pytest tests/test_readiness.py tests/test_readiness_engine.py` in
`backend`, and `npm run build` in `frontend`. The backend migration is `032_tenant_readiness`.
