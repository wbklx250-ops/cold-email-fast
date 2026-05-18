export type ApiError = {
  message: string;
  status?: number;
  details?: unknown;
};

export class HttpError extends Error {
  status: number;
  details?: unknown;

  constructor(message: string, status: number, details?: unknown) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.details = details;
  }
}

type RequestOptions = Omit<RequestInit, "body"> & {
  body?: unknown;
  params?: Record<string, string | number | boolean | undefined | null>;
};

const DEFAULT_HEADERS: Record<string, string> = {
  "Content-Type": "application/json",
};

const buildQueryString = (params?: RequestOptions["params"]): string => {
  if (!params) {
    return "";
  }

  const entries = Object.entries(params).filter(([, value]) => value !== undefined && value !== null);

  if (entries.length === 0) {
    return "";
  }

  const query = entries
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`)
    .join("&");

  return `?${query}`;
};

export const apiRequest = async <T>(
  url: string,
  options: RequestOptions = {},
): Promise<T> => {
  const { params, body, headers, ...rest } = options;
  const queryString = buildQueryString(params);

  const response = await fetch(`${url}${queryString}`,
    {
      ...rest,
      headers: {
        ...DEFAULT_HEADERS,
        ...headers,
      },
      body: body ? JSON.stringify(body) : undefined,
    },
  );

  if (!response.ok) {
    let details: unknown;

    try {
      details = await response.json();
    } catch {
      details = await response.text();
    }

    throw new HttpError("API request failed", response.status, details);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
};

// ============================================================================
// Types
// ============================================================================

// Backend DomainStatus enum values
export type DomainStatus =
  | "purchased"
  | "cf_zone_pending"
  | "cf_zone_active"
  | "zone_created"
  | "ns_updating"
  | "ns_propagating"
  | "ns_propagated"
  | "dns_configuring"
  | "tenant_linked"
  | "pending_m365"
  | "m365_verified"
  | "pending_dkim"
  | "active"
  | "problem"
  | "error"
  | "retired";

export interface Domain {
  id: string;
  name: string;
  tld: string;
  cloudflare_zone_id: string | null;
  cloudflare_nameservers: string[];
  cloudflare_zone_status: string;
  status: DomainStatus;
  nameservers_updated: boolean;
  nameservers_updated_at: string | null;
  dns_records_created: boolean;
  mx_configured: boolean;
  spf_configured: boolean;
  dmarc_configured: boolean;
  dkim_cnames_added: boolean;
  dkim_enabled: boolean;
  dkim_selector1_cname: string | null;
  dkim_selector2_cname: string | null;
  // Phase 1 tracking
  phase1_cname_added: boolean;
  phase1_dmarc_added: boolean;
  // M365 verification
  verification_txt_value: string | null;
  verification_txt_added: boolean;
  // Error tracking
  error_message: string | null;
  // Redirect URL
  redirect_url: string | null;
  redirect_configured: boolean;
  // Milestone timestamps
  ns_propagated_at: string | null;
  m365_verified_at: string | null;
  tenant_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface DomainCreate {
  name: string;
}

// Backend domain response (matches backend DomainRead schema)
export interface DomainRead {
  id: string;
  name: string;
  tld: string;
  status: string;
  cloudflare_zone_id: string | null;
  cloudflare_nameservers: string[];
  cloudflare_zone_status: string;
  nameservers_updated: boolean;
  nameservers_updated_at: string | null;
  dns_records_created: boolean;
  mx_configured: boolean;
  spf_configured: boolean;
  dmarc_configured: boolean;
  dkim_cnames_added: boolean;
  dkim_enabled: boolean;
  dkim_selector1_cname: string | null;
  dkim_selector2_cname: string | null;
  tenant_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface DomainWithNameservers {
  domain: DomainRead;
  nameservers: string[];
  message: string;
}

// ============================================================================
// API Base URL
// ============================================================================

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ============================================================================
// Domain API Functions
// ============================================================================

export const listDomains = async (
  skip = 0,
  limit = 500,
  status?: DomainStatus
): Promise<Domain[]> => {
  return apiRequest<Domain[]>(`${API_BASE}/api/v1/domains/`, {
    params: { skip, limit, status },
  });
};

export const createDomain = async (name: string): Promise<DomainWithNameservers> => {
  const domain = await apiRequest<DomainRead>(`${API_BASE}/api/v1/domains/`, {
    method: "POST",
    body: { name } as DomainCreate,
  });
  
  // Transform backend response to frontend expected format
  return {
    domain,
    nameservers: domain.cloudflare_nameservers || [],
    message: `Domain ${domain.name} added successfully. Configure nameservers at your registrar.`,
  };
};

export const getDomain = async (id: string): Promise<Domain> => {
  return apiRequest<Domain>(`${API_BASE}/api/v1/domains/${id}`);
};

export const confirmNameservers = async (id: string): Promise<Domain> => {
  return apiRequest<Domain>(`${API_BASE}/api/v1/domains/${id}/confirm-ns`, {
    method: "POST",
  });
};

export const createDnsRecords = async (id: string): Promise<{ message: string; records_created: string[] }> => {
  return apiRequest<{ message: string; records_created: string[] }>(`${API_BASE}/api/v1/domains/${id}/create-dns`, {
    method: "POST",
  });
};

export const verifyDnsRecords = async (id: string): Promise<Domain> => {
  return apiRequest<Domain>(`${API_BASE}/api/v1/domains/${id}/status`, {
    method: "GET",
  });
};

export const deleteDomain = async (id: string): Promise<{ message: string }> => {
  return apiRequest<{ message: string }>(`${API_BASE}/api/v1/domains/${id}`, {
    method: "DELETE",
  });
};

export const bulkDeleteDomains = async (ids: string[]): Promise<{ message: string; count: number }> => {
  return apiRequest<{ message: string; count: number }>(`${API_BASE}/api/v1/domains/bulk-delete`, {
    method: "POST",
    body: ids,
  });
};

// ============================================================================
// Tenant Types
// ============================================================================

// Must match backend TenantStatus enum exactly
export type TenantStatus =
  | "new"
  | "configuring"
  | "active"
  | "suspended"
  | "retired"
  | "error"
  | "imported"
  | "first_login_pending"
  | "first_login_complete"
  | "domain_linked"
  | "domain_added"
  | "m365_connected"
  | "domain_verified"
  | "dns_configuring"
  | "dns_configured"
  | "dkim_configuring"
  | "pending_dkim"
  | "dkim_enabled"
  | "mailboxes_creating"
  | "mailboxes_configuring"
  | "mailboxes_created"
  | "ready";

export interface Tenant {
  id: string;
  microsoft_tenant_id: string;
  name: string;
  onmicrosoft_domain: string;
  provider: string;
  admin_email: string;
  status: TenantStatus;
  target_mailbox_count: number;
  domain_id: string | null;
  // Licensed user
  licensed_user_upn: string | null;
  licensed_user_id: string | null;
  licensed_user_created: boolean;
  // M365 setup tracking
  first_login_completed: boolean;
  domain_added_to_m365: boolean;
  domain_verified_in_m365: boolean;
  // DNS tracking
  mx_record_added: boolean;
  spf_record_added: boolean;
  mx_value: string | null;
  spf_value: string | null;
  // DKIM tracking
  dkim_selector1_cname: string | null;
  dkim_selector2_cname: string | null;
  dkim_cnames_added: boolean;
  dkim_enabled: boolean;
  // Mailbox tracking
  mailbox_count: number;
  mailboxes_generated: boolean;
  mailboxes_created: boolean;
  mailboxes_configured: number;
  // Custom domain
  custom_domain: string | null;
  // Error tracking
  setup_step: string | null;
  setup_error: string | null;
  provider_order_id: string | null;
  // Batch relationship
  batch_id: string | null;
  // Timestamps
  created_at: string;
  updated_at: string;
}

export interface TenantCreate {
  microsoft_tenant_id: string;
  name: string;
  onmicrosoft_domain: string;
  admin_email: string;
  admin_password: string;
  domain_id?: string;
}

export interface TenantUpdate {
  name?: string;
  status?: TenantStatus;
  admin_email?: string;
  admin_password?: string;
  domain_id?: string;
}

export interface DomainBrief {
  id: string;
  name: string;
  status: string;
  domain_index_in_tenant: number;
  domain_verified_in_m365: boolean;
  dkim_enabled: boolean;
  dns_records_created: boolean;
  mx_configured: boolean;
  spf_configured: boolean;
  dmarc_configured: boolean;
  step6_complete: boolean;
  step6_mailboxes_created: number;
  error_message: string | null;
}

export interface TenantWithDomain extends Tenant {
  domain: Domain | null;
  domains: DomainBrief[];
}

// ============================================================================
// Tenant API Functions
// ============================================================================

export const listTenants = async (
  skip = 0,
  limit = 500,
  status?: TenantStatus
): Promise<Tenant[]> => {
  return apiRequest<Tenant[]>(`${API_BASE}/api/v1/tenants/`, {
    params: { skip, limit, status },
  });
};

export const createTenant = async (data: TenantCreate): Promise<Tenant> => {
  return apiRequest<Tenant>(`${API_BASE}/api/v1/tenants/`, {
    method: "POST",
    body: data,
  });
};

export const getTenant = async (id: string): Promise<TenantWithDomain> => {
  return apiRequest<TenantWithDomain>(`${API_BASE}/api/v1/tenants/${id}`);
};

export const updateTenant = async (id: string, data: TenantUpdate): Promise<Tenant> => {
  return apiRequest<Tenant>(`${API_BASE}/api/v1/tenants/${id}`, {
    method: "PATCH",
    body: data,
  });
};

export const deleteTenant = async (id: string): Promise<void> => {
  return apiRequest<void>(`${API_BASE}/api/v1/tenants/${id}`, {
    method: "DELETE",
  });
};

export const linkDomainToTenant = async (tenantId: string, domainId: string): Promise<Tenant> => {
  return apiRequest<Tenant>(`${API_BASE}/api/v1/tenants/${tenantId}/link-domain`, {
    method: "POST",
    body: { domain_id: domainId },
  });
};

export const generateMailboxesLegacy = async (
  tenantId: string,
  count: number
): Promise<{ message: string; mailboxes_created: number }> => {
  return apiRequest<{ message: string; mailboxes_created: number }>(
    `${API_BASE}/api/v1/tenants/${tenantId}/generate-mailboxes`,
    {
      method: "POST",
      body: { count },
    }
  );
};

// ============================================================================
// Mailbox Types
// ============================================================================

export type MailboxStatus = "created" | "configured" | "uploaded" | "warming" | "ready" | "suspended";

export type WarmupStage = "none" | "early" | "ramping" | "mature" | "complete";

export interface Mailbox {
  id: string;
  email: string;
  display_name: string;
  password: string;
  tenant_id: string;
  status: MailboxStatus;
  account_enabled: boolean;
  password_set: boolean;
  upn_fixed: boolean;
  delegated: boolean;
  warmup_stage: WarmupStage;
  created_at: string;
  updated_at: string;
}

export interface MailboxCreate {
  email: string;
  display_name: string;
  password: string;
  tenant_id: string;
}

export interface MailboxUpdate {
  email?: string;
  display_name?: string;
  password?: string;
  status?: MailboxStatus;
  account_enabled?: boolean;
  password_set?: boolean;
  upn_fixed?: boolean;
  delegated?: boolean;
  warmup_stage?: WarmupStage;
}

export interface MailboxWithTenant extends Mailbox {
  tenant: Tenant;
}

export interface GenerateMailboxesPersona {
  first_name: string;
  last_name: string;
}

// ============================================================================
// Mailbox API Functions
// ============================================================================

export const listMailboxes = async (
  skip = 0,
  limit = 100,
  tenantId?: string,
  status?: MailboxStatus
): Promise<Mailbox[]> => {
  return apiRequest<Mailbox[]>(`${API_BASE}/api/v1/mailboxes/`, {
    params: { skip, limit, tenant_id: tenantId, status },
  });
};

export const getMailbox = async (id: string): Promise<Mailbox> => {
  return apiRequest<Mailbox>(`${API_BASE}/api/v1/mailboxes/${id}`);
};

export const createMailbox = async (data: MailboxCreate): Promise<Mailbox> => {
  return apiRequest<Mailbox>(`${API_BASE}/api/v1/mailboxes/`, {
    method: "POST",
    body: data,
  });
};

export const updateMailbox = async (id: string, data: MailboxUpdate): Promise<Mailbox> => {
  return apiRequest<Mailbox>(`${API_BASE}/api/v1/mailboxes/${id}`, {
    method: "PATCH",
    body: data,
  });
};

export const deleteMailbox = async (id: string): Promise<{ message: string }> => {
  return apiRequest<{ message: string }>(`${API_BASE}/api/v1/mailboxes/${id}`, {
    method: "DELETE",
  });
};

export const exportMailboxCredentials = async (tenantId?: string): Promise<void> => {
  const queryString = tenantId ? `?tenant_id=${tenantId}` : "";
  const response = await fetch(`${API_BASE}/api/v1/mailboxes/export${queryString}`);

  if (!response.ok) {
    throw new HttpError("Failed to export credentials", response.status);
  }

  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "mailbox_credentials.csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.URL.revokeObjectURL(url);
};

export const generateMailboxes = async (
  tenantId: string,
  persona: GenerateMailboxesPersona
): Promise<Mailbox[]> => {
  return apiRequest<Mailbox[]>(`${API_BASE}/api/v1/mailboxes/generate/${tenantId}`, {
    method: "POST",
    body: persona,
  });
};

// ============================================================================
// Domain Bulk Operations Types
// ============================================================================

export interface BulkImportResultItem {
  domain: string;
  status: "created" | "skipped" | "failed";
  reason: string | null;
}

export interface BulkImportResult {
  total: number;
  created: number;
  skipped: number;
  failed: number;
  results: BulkImportResultItem[];
}

export interface NameserverGroup {
  nameservers: string[];
  domain_count: number;
  domains: string[];
}

export interface BulkZoneResultItem {
  domain: string;
  success: boolean;
  zone_id?: string;
  nameservers?: string[];
  error?: string;
  phase1_dns?: {
    cname_created: boolean;
    dmarc_created: boolean;
  };
}

export interface BulkZoneResult {
  total: number;
  success: number;
  failed: number;
  results: BulkZoneResultItem[];
  nameserver_groups: NameserverGroup[];
}

export interface PropagationResult {
  total_checked: number;
  propagated: number;
  pending: number;
  propagated_domains: string[];
  pending_domains: string[];
}

export interface BulkRedirectResultItem {
  domain: string;
  success: boolean;
  redirect_url?: string;
  error?: string;
  already_exists?: boolean;
}

export interface BulkRedirectResult {
  total: number;
  success: number;
  failed: number;
  results: BulkRedirectResultItem[];
}

export interface NameserverGroupsResponse {
  groups: NameserverGroup[];
  total_domains: number;
  status_filter: string | null;
}

export interface CloudflareZoneSetupResultItem {
  domain: string;
  success: boolean;
  zone_id: string | null;
  nameservers: string[];
  zone_status: string | null;
  already_existed: boolean;
  account_label: string | null;
  database_action: string | null;
  redirect_url: string | null;
  redirect_configured: boolean;
  redirect_error: string | null;
  phase1_dns?: {
    cname_created: boolean;
    dmarc_created: boolean;
    errors: string[];
  } | null;
  error: string | null;
}

export interface CloudflareZoneSetupResult {
  total: number;
  success: number;
  failed: number;
  results: CloudflareZoneSetupResultItem[];
  nameserver_groups: NameserverGroup[];
}

// ============================================================================
// Domain Bulk Operations API Functions
// ============================================================================

export const bulkImportDomains = async (file: File): Promise<BulkImportResult> => {
  const formData = new FormData();
  formData.append("file", file);
  
  const response = await fetch(`${API_BASE}/api/v1/domains/bulk-import`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let details: unknown;
    try {
      details = await response.json();
    } catch {
      details = await response.text();
    }
    throw new HttpError("Bulk import failed", response.status, details);
  }

  return (await response.json()) as BulkImportResult;
};

export const bulkCreateZones = async (domainIds?: string[]): Promise<BulkZoneResult> => {
  return apiRequest<BulkZoneResult>(`${API_BASE}/api/v1/domains/bulk-create-zones`, {
    method: "POST",
    body: domainIds || null,
  });
};

export const getNameserverGroups = async (status?: string): Promise<NameserverGroupsResponse> => {
  return apiRequest<NameserverGroupsResponse>(`${API_BASE}/api/v1/domains/nameserver-groups`, {
    params: { status },
  });
};

export const setupCloudflareZones = async (
  domains: string[],
  redirectUrl?: string,
): Promise<CloudflareZoneSetupResult> => {
  return apiRequest<CloudflareZoneSetupResult>(`${API_BASE}/api/v1/domains/cloudflare-zone-setup`, {
    method: "POST",
    body: { domains, redirect_url: redirectUrl || null },
  });
};

export const checkPropagation = async (domainIds?: string[]): Promise<PropagationResult> => {
  return apiRequest<PropagationResult>(`${API_BASE}/api/v1/domains/check-propagation`, {
    method: "POST",
    body: domainIds || null,
  });
};

export const bulkSetupRedirects = async (domainIds?: string[]): Promise<BulkRedirectResult> => {
  return apiRequest<BulkRedirectResult>(`${API_BASE}/api/v1/domains/bulk-setup-redirects`, {
    method: "POST",
    body: domainIds || null,
  });
};

export const bulkSetRedirectUrl = async (
  redirectUrl: string, 
  domainIds?: string[]
): Promise<{ updated_count: number; redirect_url: string; domains: string[] }> => {
  return apiRequest<{ updated_count: number; redirect_url: string; domains: string[] }>(
    `${API_BASE}/api/v1/domains/bulk-set-redirect`,
    {
      method: "POST",
      body: { redirect_url: redirectUrl, domain_ids: domainIds || null },
    }
  );
};

// ============================================================================
// Tenant Bulk Operations Types
// ============================================================================

export interface TenantBulkImportResultItem {
  row: number;
  tenant: string;
  status: "created" | "skipped" | "failed";
  reason: string;
}

export interface TenantBulkImportResult {
  total: number;
  created: number;
  skipped: number;
  failed: number;
  results: TenantBulkImportResultItem[];
}

export interface TenantBulkOperationResultItem {
  tenant_id: string;
  tenant_name: string;
  domain: string | null;
  success: boolean;
  steps_completed: string[];
  error: string | null;
  mx_value?: string;
  spf_value?: string;
  verification_txt?: string;
  selector1_cname?: string;
  selector2_cname?: string;
  dkim_enable_error?: string;
}

export interface TenantBulkOperationResult {
  total: number;
  processed: number;
  succeeded: number;
  failed: number;
  results: TenantBulkOperationResultItem[];
  message?: string;
}

// ============================================================================
// Tenant Bulk Operations API Functions
// ============================================================================

export const bulkImportTenantsCsv = async (file: File): Promise<TenantBulkImportResult> => {
  const formData = new FormData();
  formData.append("file", file);
  
  const response = await fetch(`${API_BASE}/api/v1/tenants/bulk-import-csv`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let details: unknown;
    try {
      details = await response.json();
    } catch {
      details = await response.text();
    }
    throw new HttpError("Tenant bulk import failed", response.status, details);
  }

  return (await response.json()) as TenantBulkImportResult;
};

export const bulkAddTenantsToM365 = async (tenantIds?: string[]): Promise<TenantBulkOperationResult> => {
  return apiRequest<TenantBulkOperationResult>(`${API_BASE}/api/v1/tenants/bulk-add-to-m365`, {
    method: "POST",
    body: tenantIds || null,
  });
};

export const bulkSetupTenantDns = async (tenantIds?: string[]): Promise<TenantBulkOperationResult> => {
  return apiRequest<TenantBulkOperationResult>(`${API_BASE}/api/v1/tenants/bulk-setup-dns`, {
    method: "POST",
    body: tenantIds || null,
  });
};

export const bulkSetupTenantDkim = async (tenantIds?: string[]): Promise<TenantBulkOperationResult> => {
  return apiRequest<TenantBulkOperationResult>(`${API_BASE}/api/v1/tenants/bulk-setup-dkim`, {
    method: "POST",
    body: tenantIds || null,
  });
};

export const bulkCreateMailboxesInM365 = async (tenantIds?: string[]): Promise<TenantBulkOperationResult> => {
  return apiRequest<TenantBulkOperationResult>(`${API_BASE}/api/v1/tenants/bulk-create-mailboxes`, {
    method: "POST",
    body: tenantIds || null,
  });
};

export const bulkConfigureMailboxes = async (tenantIds?: string[]): Promise<TenantBulkOperationResult> => {
  return apiRequest<TenantBulkOperationResult>(`${API_BASE}/api/v1/tenants/bulk-configure-mailboxes`, {
    method: "POST",
    body: tenantIds || null,
  });
};

// ============================================================================
// Wizard Types
// ============================================================================

export interface WizardStatus {
  current_step: number;
  step_name: string;
  can_proceed: boolean;
  // Step 1: Domains
  domains_total: number;
  domains_imported: boolean;
  // Step 2: Zones
  zones_created: number;
  zones_pending: number;
  // Step 3: Propagation & Redirects
  ns_propagated: number;
  ns_pending: number;
  redirects_configured: number;
  // Step 4: Tenants
  tenants_total: number;
  tenants_linked: number;
  // Step 5: M365 & DKIM
  tenants_m365_verified: number;
  tenants_dkim_enabled: number;
  // Step 6: Mailboxes
  mailboxes_total: number;
  mailboxes_pending: number;
  mailboxes_ready: number;
}

export interface WizardStepResult {
  success: boolean;
  message: string;
  details?: Record<string, unknown>;
}

export interface WizardZoneResult extends WizardStepResult {
  details?: {
    success?: number;
    failed?: number;
    nameserver_groups?: NameserverGroup[];
  };
}

// ============================================================================
// Wizard API Functions
// ============================================================================

export const getWizardStatus = async (): Promise<WizardStatus> => {
  return apiRequest<WizardStatus>(`${API_BASE}/api/v1/wizard/status`);
};

export const wizardImportDomains = async (file: File, redirectUrl: string): Promise<WizardStepResult> => {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("redirect_url", redirectUrl);
  
  const response = await fetch(`${API_BASE}/api/v1/wizard/step1/import-domains`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let details: unknown;
    try {
      details = await response.json();
    } catch {
      details = await response.text();
    }
    throw new HttpError("Domain import failed", response.status, details);
  }

  return (await response.json()) as WizardStepResult;
};

export const wizardCreateZones = async (): Promise<WizardZoneResult> => {
  return apiRequest<WizardZoneResult>(`${API_BASE}/api/v1/wizard/step2/create-zones`, {
    method: "POST",
  });
};

export const wizardCheckPropagation = async (): Promise<WizardStepResult> => {
  return apiRequest<WizardStepResult>(`${API_BASE}/api/v1/wizard/step3/check-propagation`, {
    method: "POST",
  });
};

export const wizardSetupRedirects = async (): Promise<WizardStepResult> => {
  return apiRequest<WizardStepResult>(`${API_BASE}/api/v1/wizard/step3/setup-redirects`, {
    method: "POST",
  });
};

export const wizardImportTenants = async (file: File): Promise<WizardStepResult> => {
  const formData = new FormData();
  formData.append("file", file);
  
  const response = await fetch(`${API_BASE}/api/v1/wizard/step4/import-tenants`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let details: unknown;
    try {
      details = await response.json();
    } catch {
      details = await response.text();
    }
    throw new HttpError("Tenant import failed", response.status, details);
  }

  return (await response.json()) as WizardStepResult;
};

export const wizardSetupM365 = async (): Promise<WizardStepResult> => {
  return apiRequest<WizardStepResult>(`${API_BASE}/api/v1/wizard/step5/setup-m365`, {
    method: "POST",
  });
};

export const wizardSetupDkim = async (): Promise<WizardStepResult> => {
  return apiRequest<WizardStepResult>(`${API_BASE}/api/v1/wizard/step5/setup-dkim`, {
    method: "POST",
  });
};

export const wizardGenerateMailboxes = async (
  firstName: string,
  lastName: string,
  count: number = 50
): Promise<WizardStepResult> => {
  const formData = new FormData();
  formData.append("first_name", firstName);
  formData.append("last_name", lastName);
  formData.append("count", count.toString());
  
  const response = await fetch(`${API_BASE}/api/v1/wizard/step6/generate-mailboxes`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    let details: unknown;
    try {
      details = await response.json();
    } catch {
      details = await response.text();
    }
    throw new HttpError("Mailbox generation failed", response.status, details);
  }

  return (await response.json()) as WizardStepResult;
};

export const wizardCreateMailboxes = async (): Promise<WizardStepResult> => {
  return apiRequest<WizardStepResult>(`${API_BASE}/api/v1/wizard/step6/create-mailboxes`, {
    method: "POST",
  });
};

export const wizardExportCredentials = async (): Promise<void> => {
  const response = await fetch(`${API_BASE}/api/v1/wizard/step6/export-credentials`);

  if (!response.ok) {
    throw new HttpError("Failed to export credentials", response.status);
  }

  const blob = await response.blob();
  const url = window.URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "mailbox_credentials.csv";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  window.URL.revokeObjectURL(url);
};

// ============================================================================
// Mailbox Password Reset (UI trigger)
// ============================================================================

export interface ResetPasswordsStatus {
  status: "idle" | "running" | "completed" | "failed";
  total: number;
  completed: number;
  failed: number;
  current_tenant: string | null;
  errors: string[];
}

export const resetAllMailboxPasswords = async (
  tenantId?: string,
): Promise<{ status: string; message: string }> => {
  return apiRequest<{ status: string; message: string }>(
    `${API_BASE}/api/v1/mailboxes/reset-all-passwords`,
    {
      method: "POST",
      params: { tenant_id: tenantId },
    },
  );
};

export const getResetPasswordsStatus = async (): Promise<ResetPasswordsStatus> => {
  return apiRequest<ResetPasswordsStatus>(
    `${API_BASE}/api/v1/mailboxes/reset-passwords-status`,
  );
};

// ============================================================================
// Batch Upload Tracking API Functions (Feature 3)
// ============================================================================

export const markBatchUploaded = async (batchId: string): Promise<{ message: string }> => {
  return apiRequest<{ message: string }>(`${API_BASE}/api/v1/wizard/batches/${batchId}/mark-uploaded`, {
    method: "POST",
  });
};

export const unmarkBatchUploaded = async (batchId: string): Promise<{ message: string }> => {
  return apiRequest<{ message: string }>(`${API_BASE}/api/v1/wizard/batches/${batchId}/unmark-uploaded`, {
    method: "POST",
  });
};

// ============================================================================
// Auto-Run API Functions (Feature 2 - Auto-progression)
// ============================================================================

export interface AutoRunRequest {
  new_password: string;
  display_name: string;
  sequencer_app_key?: string;
}

export interface AutoRunStatus {
  batch_id: string;
  status: "idle" | "running" | "completed" | "failed" | "stopped";
  current_step: number;
  current_step_name: string;
  progress: {
    step4: { completed: number; failed: number; total: number };
    step5: { completed: number; failed: number; total: number };
    step6: { completed: number; failed: number; total: number };
    step7: { completed: number; failed: number; total: number };
  };
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  message: string;
}

export const startAutoRun = async (
  batchId: string,
  request: AutoRunRequest
): Promise<{ success: boolean; message: string; batch_id: string }> => {
  return apiRequest<{ success: boolean; message: string; batch_id: string }>(
    `${API_BASE}/api/v1/wizard/batches/${batchId}/auto-run`,
    {
      method: "POST",
      body: request,
    }
  );
};

export const getAutoRunStatus = async (batchId: string): Promise<AutoRunStatus> => {
  return apiRequest<AutoRunStatus>(`${API_BASE}/api/v1/wizard/batches/${batchId}/auto-run/status`);
};

export const stopAutoRun = async (batchId: string): Promise<{ success: boolean; message: string }> => {
  return apiRequest<{ success: boolean; message: string }>(
    `${API_BASE}/api/v1/wizard/batches/${batchId}/auto-run/stop`,
    {
      method: "POST",
    }
  );
};
