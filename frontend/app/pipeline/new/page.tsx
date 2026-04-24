"use client";

import { useState, useCallback, useEffect } from "react";
import { useRouter } from "next/navigation";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface ValidationResult {
  valid: boolean;
  errors: string[];
  warnings: string[];
  summary: {
    domains_count: number;
    tenants_count: number;
    tenants_used?: number;
    domains_per_tenant: number;
    credentials_matched: number;
    credentials_unmatched: number;
    domains_linked: number;
    expected_mailboxes: number;
  };
}

interface SavedAccount {
  id: string;
  label: string;
  email?: string;
  platform?: string;
}

// Platform-specific credential fields
const PLATFORM_FIELDS: Record<string, { label: string; key: string; type: string }[]> = {
  instantly: [
    { label: "Label", key: "label", type: "text" },
    { label: "Email", key: "email", type: "email" },
    { label: "Password", key: "password", type: "password" },
    { label: "API Key (optional)", key: "api_key", type: "text" },
  ],
  smartlead: [
    { label: "Label", key: "label", type: "text" },
    { label: "API Key", key: "api_key", type: "text" },
    { label: "OAuth URL", key: "oauth_url", type: "text" },
  ],
  plusvibe: [
    { label: "Label", key: "label", type: "text" },
    { label: "Email", key: "email", type: "email" },
    { label: "Password", key: "password", type: "password" },
  ],
};

export default function NewPipelinePage() {
  const router = useRouter();

  // Form state
  const [batchName, setBatchName] = useState("");
  const [domainsPerTenant, setDomainsPerTenant] = useState(1);
  const [mailboxesPerTenant, setMailboxesPerTenant] = useState(50);
  const [domainsCsv, setDomainsCsv] = useState<File | null>(null);
  const [tenantsCsv, setTenantsCsv] = useState<File | null>(null);
  const [credentialsTxt, setCredentialsTxt] = useState<File | null>(null);
  const [profilePhoto, setProfilePhoto] = useState<File | null>(null);
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");

  // Sequencer state
  const [sequencerPlatform, setSequencerPlatform] = useState("");
  const [savedAccounts, setSavedAccounts] = useState<SavedAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [addNewAccount, setAddNewAccount] = useState(false);
  const [newAccountFields, setNewAccountFields] = useState<Record<string, string>>({});
  const [saveNewAccount, setSaveNewAccount] = useState(true);
  const [sequencerApiKey, setSequencerApiKey] = useState("");

  // UI state
  const [validation, setValidation] = useState<ValidationResult | null>(null);
  const [isValidating, setIsValidating] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isSavingAccount, setIsSavingAccount] = useState(false);

  // Fetch saved accounts when platform changes
  useEffect(() => {
    if (sequencerPlatform === "instantly" || sequencerPlatform === "smartlead" || sequencerPlatform === "plusvibe") {
      fetch(`${API_BASE}/api/v1/step8/instantly/accounts`)
        .then(res => res.json())
        .then(data => setSavedAccounts(data.accounts || []))
        .catch(() => setSavedAccounts([]));
    } else {
      setSavedAccounts([]);
    }
    setSelectedAccountId("");
    setAddNewAccount(false);
    setNewAccountFields({});
    setSequencerApiKey("");
  }, [sequencerPlatform]);

  // Validate files
  const runValidation = useCallback(async () => {
    if (!domainsCsv || !tenantsCsv) return;

    setIsValidating(true);
    setError(null);

    try {
      const formData = new FormData();
      formData.append("domains_csv", domainsCsv);
      formData.append("tenants_csv", tenantsCsv);
      if (credentialsTxt) formData.append("credentials_txt", credentialsTxt);
      formData.append("first_name", firstName);
      formData.append("last_name", lastName);
      formData.append("domains_per_tenant", domainsPerTenant.toString());
      formData.append("mailboxes_per_tenant", mailboxesPerTenant.toString());

      const res = await fetch(`${API_BASE}/api/v1/pipeline/validate`, {
        method: "POST",
        body: formData,
      });
      const data: ValidationResult = await res.json();
      setValidation(data);
    } catch {
      setError("Validation request failed");
    } finally {
      setIsValidating(false);
    }
  }, [domainsCsv, tenantsCsv, credentialsTxt, firstName, lastName, domainsPerTenant, mailboxesPerTenant]);

  // Save new sequencer account via API, return account ID
  const saveSequencerAccount = async (): Promise<string> => {
    if (!saveNewAccount || !newAccountFields.label) return "";

    setIsSavingAccount(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/step8/instantly/accounts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: sequencerPlatform,
          label: newAccountFields.label || "",
          email: newAccountFields.email || "",
          password: newAccountFields.password || "",
          api_key: newAccountFields.api_key || "",
          oauth_url: newAccountFields.oauth_url || "",
        }),
      });
      if (res.ok) {
        const data = await res.json();
        return data.account_id || data.id || "";
      }
    } catch (e) {
      console.error("Failed to save sequencer account:", e);
    } finally {
      setIsSavingAccount(false);
    }
    return "";
  };

  const handleSubmit = async () => {
    if (!batchName || !domainsCsv || !tenantsCsv) {
      setError("Please fill in all required fields");
      return;
    }

    setIsSubmitting(true);
    setError(null);

    try {
      // If adding a new account and save is checked, save it first
      let accountId = selectedAccountId || "";
      let apiKey = sequencerApiKey || newAccountFields.api_key || "";

      if (sequencerPlatform && addNewAccount && saveNewAccount && newAccountFields.label) {
        const savedId = await saveSequencerAccount();
        if (savedId) accountId = savedId;
      }

      const formData = new FormData();
      formData.append("batch_name", batchName);
      formData.append("domains_csv", domainsCsv);
      formData.append("tenants_csv", tenantsCsv);
      if (credentialsTxt) formData.append("credentials_txt", credentialsTxt);
      formData.append("first_name", firstName);
      formData.append("last_name", lastName);
      formData.append("domains_per_tenant", domainsPerTenant.toString());
      formData.append("mailboxes_per_tenant", mailboxesPerTenant.toString());
      formData.append("sequencer_platform", sequencerPlatform);
      formData.append("sequencer_account_id", accountId);
      formData.append("sequencer_api_key", apiKey);
      if (profilePhoto) {
        formData.append("profile_photo", profilePhoto);
      }

      const res = await fetch(`${API_BASE}/api/v1/pipeline/create-and-start`, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const errData = await res.json();
        throw new Error(errData.detail?.errors?.join(", ") || "Failed to create batch");
      }

      const data = await res.json();
      router.push(`/pipeline/${data.batch_id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to start pipeline");
    } finally {
      setIsSubmitting(false);
    }
  };

  const allFilesUploaded = domainsCsv && tenantsCsv;
  const canSubmit = batchName && allFilesUploaded && validation?.valid;

  return (
    <div className="max-w-3xl mx-auto py-8 px-4">
      <h1 className="text-2xl font-bold text-gray-900 mb-2">New Batch Setup</h1>
      <p className="text-gray-600 mb-8">
        Upload all files and configuration. The system will run the entire pipeline automatically.
      </p>

      {error && (
        <div className="mb-6 rounded-lg bg-red-50 border border-red-200 p-4 text-red-800 text-sm">
          {error}
        </div>
      )}

      <div className="space-y-8">
        {/* Section 1: Batch Info */}
        <div>
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Batch Info</h2>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Batch Name <span className="text-red-500">*</span>
              </label>
              <input
                type="text"
                value={batchName}
                onChange={(e) => setBatchName(e.target.value)}
                placeholder="e.g., Client ABC - March 2026"
                className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Domains per Tenant
              </label>
              <select
                value={domainsPerTenant}
                onChange={(e) => { setDomainsPerTenant(Number(e.target.value)); setValidation(null); }}
                className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              >
                <option value={1}>1 domain per tenant</option>
                <option value={2}>2 domains per tenant</option>
                <option value={3}>3 domains per tenant</option>
              </select>
              <p className="mt-1 text-xs text-gray-400">
                How many custom domains to assign to each M365 tenant. Default is 1.
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Mailboxes per Domain
              </label>
              <input
                type="number"
                min={25}
                max={100}
                step={1}
                value={mailboxesPerTenant}
                onChange={(e) => {
                  const raw = Number(e.target.value);
                  const clamped = Math.max(25, Math.min(100, isNaN(raw) ? 50 : raw));
                  setMailboxesPerTenant(clamped);
                  setValidation(null);
                }}
                className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              />
              <p className="mt-1 text-xs text-gray-400">
                How many mailboxes to create per custom domain (25–100). Default is 50.
              </p>
            </div>
          </div>
        </div>

        {/* Section 2: Upload Files */}
        <div>
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Upload Files</h2>
          <div className="space-y-4">
            <FileUpload
              label="Domains CSV"
              accept=".csv"
              required
              file={domainsCsv}
              onFile={(f) => { setDomainsCsv(f); setValidation(null); }}
              hint="Columns: domain, redirect_url, first_name, last_name — first_name/last_name optional per row; blank rows use the default below."
            />
            <FileUpload
              label="Tenants CSV"
              accept=".csv"
              required
              file={tenantsCsv}
              onFile={(f) => { setTenantsCsv(f); setValidation(null); }}
              hint="From reseller — company name, onmicrosoft domain, etc."
            />
            <FileUpload
              label="Credentials TXT"
              accept=".txt"
              file={credentialsTxt}
              onFile={(f) => { setCredentialsTxt(f); setValidation(null); }}
              hint="Optional — Username: / Password: pairs from reseller"
            />
            <FileUpload
              label="Profile Photo"
              accept=".jpg,.jpeg,.png"
              file={profilePhoto}
              onFile={setProfilePhoto}
              hint="Optional — same photo for all mailboxes"
            />
          </div>
        </div>

        {/* Section 3: Mailbox Configuration */}
        <div>
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Mailbox Configuration</h2>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Default First Name
              </label>
              <input
                type="text"
                value={firstName}
                onChange={(e) => { setFirstName(e.target.value); setValidation(null); }}
                placeholder="Jack"
                className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Default Last Name
              </label>
              <input
                type="text"
                value={lastName}
                onChange={(e) => { setLastName(e.target.value); setValidation(null); }}
                placeholder="Zuvelek"
                className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              />
            </div>
          </div>
          <p className="mt-2 text-xs text-gray-500">
            Default persona for all domains. Override per-domain by adding <code className="font-mono">first_name</code> and <code className="font-mono">last_name</code> columns to your domains CSV.
          </p>
        </div>

        {/* Section 4: Sequencer (Optional) */}
        <div>
          <h2 className="text-lg font-semibold text-gray-900 mb-4">Sequencer (Optional)</h2>
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Platform</label>
              <select
                value={sequencerPlatform}
                onChange={(e) => setSequencerPlatform(e.target.value)}
                className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
              >
                <option value="">— Skip sequencer upload —</option>
                <option value="instantly">Instantly</option>
                <option value="smartlead">Smartlead</option>
                <option value="plusvibe">PlusVibe</option>
              </select>
            </div>

            {sequencerPlatform && (
              <div className="rounded-lg border border-gray-200 bg-gray-50 p-4 space-y-4">
                {/* Saved accounts dropdown */}
                {savedAccounts.length > 0 && !addNewAccount && (
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Saved Account</label>
                    <select
                      value={selectedAccountId}
                      onChange={(e) => setSelectedAccountId(e.target.value)}
                      className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 bg-white"
                    >
                      <option value="">— Select a saved account —</option>
                      {savedAccounts.map((acc) => (
                        <option key={acc.id} value={acc.id}>
                          {acc.label || acc.email || acc.id}
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                {/* Toggle: Add New Account */}
                <button
                  type="button"
                  onClick={() => {
                    setAddNewAccount(!addNewAccount);
                    if (!addNewAccount) setSelectedAccountId("");
                  }}
                  className="text-sm text-blue-600 hover:text-blue-800 font-medium"
                >
                  {addNewAccount ? "← Use saved account" : "+ Add New Account"}
                </button>

                {/* New account fields */}
                {addNewAccount && (
                  <div className="space-y-3">
                    {(PLATFORM_FIELDS[sequencerPlatform] || []).map((field) => (
                      <div key={field.key}>
                        <label className="block text-sm font-medium text-gray-700 mb-1">
                          {field.label}
                        </label>
                        <input
                          type={field.type}
                          value={newAccountFields[field.key] || ""}
                          onChange={(e) =>
                            setNewAccountFields((prev) => ({ ...prev, [field.key]: e.target.value }))
                          }
                          className="w-full rounded-lg border border-gray-300 px-4 py-2.5 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                        />
                      </div>
                    ))}

                    <label className="flex items-center gap-2 text-sm text-gray-600 mt-2">
                      <input
                        type="checkbox"
                        checked={saveNewAccount}
                        onChange={(e) => setSaveNewAccount(e.target.checked)}
                        className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                      />
                      Save this account for future batches
                    </label>
                  </div>
                )}

                {/* No saved accounts message */}
                {savedAccounts.length === 0 && !addNewAccount && (
                  <p className="text-sm text-gray-500">
                    No saved accounts found. Click &quot;Add New Account&quot; to add one.
                  </p>
                )}
              </div>
            )}
          </div>
        </div>

        {/* Validate Button */}
        {allFilesUploaded && !validation && (
          <button
            onClick={runValidation}
            disabled={isValidating}
            className="w-full rounded-lg bg-gray-100 border border-gray-300 py-3 text-sm font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50"
          >
            {isValidating ? "Validating..." : "Validate Files"}
          </button>
        )}

        {/* Validation Preview */}
        {validation && (
          <div className={`rounded-lg border p-5 ${validation.valid ? "bg-green-50 border-green-200" : "bg-red-50 border-red-200"}`}>
            <h3 className="font-semibold text-gray-900 mb-3">
              {validation.valid ? "✓ Ready to start" : "✗ Fix errors before starting"}
            </h3>

            {validation.errors.length > 0 && (
              <div className="mb-3">
                {validation.errors.map((err, i) => (
                  <p key={i} className="text-sm text-red-700">❌ {err}</p>
                ))}
              </div>
            )}

            {validation.warnings.length > 0 && (
              <div className="mb-3">
                {validation.warnings.map((warn, i) => (
                  <p key={i} className="text-sm text-yellow-700">⚠️ {warn}</p>
                ))}
              </div>
            )}

            {/* Domain ÷ Tenant formula */}
            {validation.valid && (
              <div className="mb-3 space-y-1">
                <p className="text-sm text-gray-700 font-mono">
                  {validation.summary.domains_count} domains ÷ {validation.summary.domains_per_tenant || domainsPerTenant} per tenant = {validation.summary.tenants_used ?? validation.summary.tenants_count} tenant(s) used ✓
                </p>
                <p className="text-sm text-gray-700 font-mono">
                  Expected mailboxes: {validation.summary.domains_count} domains × {mailboxesPerTenant} = {(validation.summary.expected_mailboxes ?? 0).toLocaleString()}
                </p>
              </div>
            )}

            <div className="grid grid-cols-3 gap-4 text-center mt-4">
              <div className="bg-white rounded-lg p-3 shadow-sm">
                <p className="text-2xl font-bold text-blue-600">{validation.summary.domains_count}</p>
                <p className="text-xs text-gray-500">Domains</p>
              </div>
              <div className="bg-white rounded-lg p-3 shadow-sm">
                <p className="text-2xl font-bold text-blue-600">{validation.summary.tenants_count}</p>
                <p className="text-xs text-gray-500">Tenants</p>
              </div>
              <div className="bg-white rounded-lg p-3 shadow-sm">
                <p className="text-2xl font-bold text-blue-600">{validation.summary.domains_per_tenant || domainsPerTenant}</p>
                <p className="text-xs text-gray-500">Domains/Tenant</p>
              </div>
              <div className="bg-white rounded-lg p-3 shadow-sm">
                <p className="text-2xl font-bold text-blue-600">{validation.summary.credentials_matched}</p>
                <p className="text-xs text-gray-500">Creds Matched</p>
              </div>
              <div className="bg-white rounded-lg p-3 shadow-sm col-span-2">
                <p className="text-2xl font-bold text-purple-600">{(validation.summary.expected_mailboxes ?? 0).toLocaleString()}</p>
                <p className="text-xs text-gray-500">Mailboxes to create</p>
              </div>
            </div>
          </div>
        )}

        {/* Submit Button */}
        <button
          onClick={handleSubmit}
          disabled={!canSubmit || isSubmitting || isSavingAccount}
          className="w-full rounded-lg bg-blue-600 py-3.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:bg-gray-300 disabled:cursor-not-allowed transition-colors"
        >
          {isSavingAccount ? "Saving Account..." : isSubmitting ? "Starting Pipeline..." : "🚀 Start Batch Setup"}
        </button>
      </div>
    </div>
  );
}

// Simple file upload component
function FileUpload({
  label,
  accept,
  required,
  file,
  onFile,
  hint,
}: {
  label: string;
  accept: string;
  required?: boolean;
  file: File | null;
  onFile: (f: File | null) => void;
  hint?: string;
}) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1">
        {label} {required && <span className="text-red-500">*</span>}
      </label>
      <div className="flex items-center gap-3">
        <label className="cursor-pointer rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm text-gray-600 hover:bg-gray-50">
          Choose File
          <input
            type="file"
            accept={accept}
            className="hidden"
            onChange={(e) => onFile(e.target.files?.[0] || null)}
          />
        </label>
        {file ? (
          <span className="text-sm text-green-700 font-medium">✓ {file.name}</span>
        ) : (
          <span className="text-sm text-gray-400">No file selected</span>
        )}
      </div>
      {hint && <p className="mt-1 text-xs text-gray-400">{hint}</p>}
    </div>
  );
}
