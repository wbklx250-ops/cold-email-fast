# Domain removal

Both database and CSV removal use the same recipient cleanup gate. No swap worker,
swap job, or replacement batch is created.

1. Read all Graph users and active Exchange recipients. Protect the signed-in
   administrator, synchronized users, licensed shared mailboxes, and recipients
   that reference another custom domain.
2. Delete shared-mailbox user objects belonging to the selected domain and verify
   both Graph absence and Exchange propagation. Microsoft Graph user deletion also
   removes the associated mailbox from active use; normal Microsoft retention applies.
3. Unassign licenses from the domain's recorded application user (or `me1` user),
   verify the release, and delete that user by its Microsoft object ID.
4. Preserve Microsoft 365 groups by moving their old-domain primary address to
   the tenant's initial domain and removing only their old-domain SMTP aliases.
5. Verify Exchange no longer references the deleted users or old domain. Only
   then run the existing verified domain removal and DNS cleanup.

Object IDs identify mailboxes renamed by prior force-delete attempts. Legacy DB
records without IDs can match their original local part on the **same tenant's
verified initial `.onmicrosoft.com` domain**. Display names and addresses on other
custom domains are not used for this recovery. CSV-only removal cannot recover
renamed mailboxes without matching database records.

Incomplete discovery, permissions/authentication errors, or propagation failures
stop removal and preserve DNS and database links for retry. Partial deletions are
safe to retry; a Graph 404 means the user is already absent. Exchange credentials
must support the same non-interactive authentication used for mailbox creation.
Cleanup does not bypass MFA or retention policies and does not purge deleted users.

References: [Graph user deletion](https://learn.microsoft.com/en-us/graph/api/user-delete?view=graph-rest-1.0),
[Exchange mailbox deletion](https://learn.microsoft.com/en-us/exchange/recipients-in-exchange-online/delete-or-restore-mailboxes).
