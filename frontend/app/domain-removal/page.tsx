"use client";

import { useState, useRef, useCallback, useEffect } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface ValidationResult {
  domain: string;
  can_remove: boolean;
  reason: string;
  source: string;
  tenant?: {
    id?: string;
    name?: string;
    onmicrosoft_domain?: string;
    admin_email?: string;
  };
  mailbox_count: number | string;
  has_totp?: boolean;
  row?: number;
}

interface RemovalStepResult {
  success?: boolean;
  skipped?: boolean;
  error?: string;
  note?: string;
  removed?: number;
  failed?: number;
  records_deleted?: number;
  records_failed?: number;
  mailboxes_archived?: number;
  upns_reset?: number;
}

interface RemovalResult {
  domain: string;
  source: string;
  success: boolean;
  error?: string;
  warning?: string;
  steps: Record<string, RemovalStepResult>;
}

interface JobStatus {
  id: string;
  mode: string;
  status: string;
  started_at: string;
  completed_at?: string;
  total: number;
  completed: number;
  successful: number;
  failed: number;
  domains: string[];
  results: RemovalResult[];
}

type Mode = "db" | "csv";
type Phase = "input" | "validated" | "removing" | "done";

export default function DomainRemovalPage() {
  const [mode, setMode] = useState<Mode>("db");
  const [phase, setPhase] = useState<Phase>("input");
  const [domainInput, setDomainInput] = useState("");
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [validationResults, setValidationResults] = useState<ValidationResult[]>([]);
  const [removalResults, setRemovalResults] = useState<RemovalResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [skipM365, setSkipM365] = useState(false);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<NodeJS.Timeout | null>(null);

  // Clean up polling on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const getDomains = useCallback(() => {
    return domainInput
      .split(/[\n,]/)
      .map((d) => d.trim().toLowerCase())
      .filter((d) => d.length > 0 && d.includes("."));
  }, [domainInput]);

  const handleValidateDB = async () => {
    const domains = getDomains();
    if (!domains.length) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/domain-removal/validate-db`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ domains }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setValidationResults(data.domains || []);
      setPhase("validated");
    } catch (err) {
      setError("Validation failed: " + (err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleValidateCSV = async () => {
    if (!csvFile) return;
    setLoading(true);
    setError(null);
    try {
      const formData = new FormData();
      formData.append("file", csvFile);
      const res = await fetch(`${API_BASE}/api/v1/domain-removal/validate-csv`, {
        method: "POST",
        body: formData,
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setValidationResults(data.domains || []);
      setPhase("validated");
    } catch (err) {
      setError("CSV validation failed: " + (err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  const handleRemove = async () => {
    const removable = validationResults.filter((r) => r.can_remove);
    if (!removable.length) {
      setError("No domains eligible for removal");
      return;
    }

    const confirmed = confirm(
      `Remove ${removable.length} domain(s) from their tenants?\n\n` +
        "This will:\n" +
        "• Delete shared mailboxes belonging to these domains\n" +
        "• Unassign licenses from the old application users\n" +
        "• Delete those previously licensed users\n" +
        "• Move Microsoft 365 group addresses off the old domains (preserve groups)\n" +
        "• Remove domains from M365 tenants\n" +
        "• Clean up Cloudflare DNS records\n" +
        "• Update the database\n\n" +
        "Previously renamed mailboxes are matched against this domain's stored records. Other domains are preserved."
    );
    if (!confirmed) return;

    setPhase("removing");
    setLoading(true);
    setError(null);

    try {
      let res: Response;

      if (mode === "db") {
        res = await fetch(`${API_BASE}/api/v1/domain-removal/remove-db`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            domains: removable.map((r) => r.domain),
            skip_m365: skipM365,
            headless: true,
            stagger_seconds: 10,
          }),
        });
      } else {
        const formData = new FormData();
        formData.append("file", csvFile!);
        formData.append("skip_m365", String(skipM365));
        formData.append("headless", "true");
        formData.append("stagger_seconds", "10");
        res = await fetch(`${API_BASE}/api/v1/domain-removal/remove-csv`, {
          method: "POST",
          body: formData,
        });
      }

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      const jobId = data.job_id;

      // Start polling for job status
      pollRef.current = setInterval(async () => {
        try {
          const statusRes = await fetch(
            `${API_BASE}/api/v1/domain-removal/jobs/${jobId}`
          );
          if (!statusRes.ok) return;
          const statusData: JobStatus = await statusRes.json();
          setJobStatus(statusData);

          if (statusData.status === "completed") {
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = null;
            setRemovalResults(statusData.results || []);
            setPhase("done");
            setLoading(false);
          }
        } catch {
          // Polling error - will retry on next interval
        }
      }, 5000);
    } catch (err) {
      setError("Removal failed: " + (err as Error).message);
      setPhase("validated");
      setLoading(false);
    }
  };

  const resetAll = () => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = null;
    setPhase("input");
    setDomainInput("");
    setCsvFile(null);
    setValidationResults([]);
    setRemovalResults([]);
    setJobStatus(null);
    setError(null);
    setLoading(false);
  };

  const removableCount = validationResults.filter((r) => r.can_remove).length;
  const notRemovableCount = validationResults.filter((r) => !r.can_remove).length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Domain Removal</h1>
        <p className="text-sm text-gray-500 mt-1">
          Remove domains from M365 tenants so they can be reassigned to new
          tenants
        </p>
      </div>

      {/* Error Banner */}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <div className="flex items-center justify-between">
            <p className="text-sm text-red-700">{error}</p>
            <button
              onClick={() => setError(null)}
              className="text-red-400 hover:text-red-600"
            >
              ✕
            </button>
          </div>
        </div>
      )}

      {/* Mode Tabs */}
      {(phase === "input" || phase === "validated") && (
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
          <button
            onClick={() => {
              setMode("db");
              resetAll();
            }}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              mode === "db"
                ? "bg-white shadow text-gray-900"
                : "text-gray-500 hover:text-gray-700"
            }`}
          >
            From Database
          </button>
          <button
            onClick={() => {
              setMode("csv");
              resetAll();
            }}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              mode === "csv"
                ? "bg-white shadow text-gray-900"
                : "text-gray-500 hover:text-gray-700"
            }`}
          >
            From CSV (External)
          </button>
        </div>
      )}

      {/* Input Section */}
      {(phase === "input" || phase === "validated") && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          {mode === "db" ? (
            <>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Enter domains (one per line or comma-separated)
              </label>
              <p className="text-xs text-gray-400 mb-2">
                These must exist in the database with linked tenants. Credentials
                will be looked up automatically.
              </p>
              <textarea
                className="w-full h-40 border border-gray-300 rounded-lg p-3 text-sm font-mono focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                placeholder={"example1.com\nexample2.com\nexample3.com"}
                value={domainInput}
                onChange={(e) => {
                  setDomainInput(e.target.value);
                  if (phase === "validated") setPhase("input");
                }}
              />
              {domainInput && (
                <p className="text-xs text-gray-400 mt-1">
                  {getDomains().length} domain(s) entered
                </p>
              )}
            </>
          ) : (
            <>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Upload CSV with domain + tenant credentials
              </label>
              <p className="text-xs text-gray-400 mb-3">
                For domains NOT in our database (external/client tenants). Leave
                totp_secret empty if MFA is disabled.
              </p>

              {/* CSV Format Example */}
              <div className="bg-gray-50 border border-gray-200 rounded-lg p-3 mb-4">
                <p className="text-xs font-medium text-gray-600 mb-1">
                  CSV Format:
                </p>
                <code className="text-xs text-gray-500 block font-mono whitespace-pre">
                  {`domain,admin_email,admin_password,totp_secret\nexample.com,admin@contoso.onmicrosoft.com,Password123,\nexample2.com,admin@fabrikam.onmicrosoft.com,Pass456!,JBSWY3DPEHPK3PXP`}
                </code>
              </div>

              {/* File Upload */}
              <div className="flex items-center gap-3">
                <div
                  onClick={() => fileInputRef.current?.click()}
                  className={`flex-1 border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors ${
                    csvFile
                      ? "border-green-400 bg-green-50"
                      : "border-gray-300 hover:border-gray-400 hover:bg-gray-50"
                  }`}
                >
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".csv"
                    className="hidden"
                    onChange={(e) => {
                      setCsvFile(e.target.files?.[0] || null);
                      if (phase === "validated") setPhase("input");
                    }}
                  />
                  {csvFile ? (
                    <div>
                      <p className="text-sm font-medium text-green-700">
                        ✓ {csvFile.name}
                      </p>
                      <p className="text-xs text-green-600 mt-1">
                        {(csvFile.size / 1024).toFixed(1)} KB
                      </p>
                    </div>
                  ) : (
                    <div>
                      <p className="text-sm text-gray-500">
                        Click to upload CSV file
                      </p>
                      <p className="text-xs text-gray-400 mt-1">
                        Required: domain, admin_email, admin_password
                      </p>
                    </div>
                  )}
                </div>
                <button
                  onClick={() =>
                    window.open(
                      `${API_BASE}/api/v1/domain-removal/csv-template`,
                      "_blank"
                    )
                  }
                  className="text-sm text-blue-600 hover:text-blue-800 underline whitespace-nowrap"
                >
                  Download Template
                </button>
              </div>
            </>
          )}

          {/* Options */}
          <div className="mt-4 flex items-center gap-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={skipM365}
                onChange={(e) => setSkipM365(e.target.checked)}
                className="rounded border-gray-300"
              />
              <span className="text-gray-700">
                Skip M365 operations
              </span>
              <span className="text-xs text-gray-400">
                (tenant suspended — only clean DB + Cloudflare)
              </span>
            </label>
          </div>

          {/* Action Buttons */}
          <div className="mt-4 flex gap-3">
            <button
              onClick={mode === "db" ? handleValidateDB : handleValidateCSV}
              disabled={
                loading || (mode === "db" ? !getDomains().length : !csvFile)
              }
              className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
            >
              {loading
                ? "Validating..."
                : mode === "db"
                  ? `Validate ${getDomains().length} Domain(s)`
                  : "Validate CSV"}
            </button>

            {phase === "validated" && removableCount > 0 && (
              <button
                onClick={handleRemove}
                disabled={loading}
                className="px-4 py-2 bg-red-600 text-white rounded-lg hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
              >
                🗑️ Remove {removableCount} Domain(s)
              </button>
            )}
          </div>
        </div>
      )}

      {/* Validation Results */}
      {phase === "validated" && validationResults.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="text-lg font-semibold mb-4">
            Validation Results:{" "}
            <span className="text-green-600">{removableCount} ready</span>
            {notRemovableCount > 0 && (
              <span className="text-red-600">
                , {notRemovableCount} issue(s)
              </span>
            )}
          </h2>

          <div className="space-y-3">
            {validationResults.map((r, i) => (
              <div
                key={i}
                className={`p-4 rounded-lg border ${
                  r.can_remove
                    ? "bg-green-50 border-green-200"
                    : "bg-red-50 border-red-200"
                }`}
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono font-medium text-gray-900">
                      {r.domain}
                    </span>
                    <span
                      className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                        r.can_remove
                          ? "bg-green-100 text-green-700"
                          : "bg-red-100 text-red-700"
                      }`}
                    >
                      {r.can_remove ? "Ready" : "Cannot Remove"}
                    </span>
                    {mode === "csv" && r.has_totp !== undefined && (
                      <span
                        className={`text-xs px-2 py-0.5 rounded-full ${
                          r.has_totp
                            ? "bg-blue-100 text-blue-700"
                            : "bg-gray-100 text-gray-600"
                        }`}
                      >
                        {r.has_totp ? "Has MFA" : "No MFA"}
                      </span>
                    )}
                    {r.row && (
                      <span className="text-xs text-gray-400">
                        Row {r.row}
                      </span>
                    )}
                  </div>
                  {r.tenant && (
                    <span className="text-sm text-gray-500">
                      {r.tenant.name ||
                        r.tenant.onmicrosoft_domain ||
                        r.tenant.admin_email}
                    </span>
                  )}
                </div>
                <p className="text-sm text-gray-600 mt-1">{r.reason}</p>
                {r.mailbox_count !== undefined &&
                  r.mailbox_count !== 0 &&
                  r.mailbox_count !== "0" && (
                    <p className="text-sm text-amber-600 mt-1">
                      📬 Mailboxes: {r.mailbox_count}
                    </p>
                  )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Progress Section */}
      {phase === "removing" && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="text-lg font-semibold mb-4">Removing Domains...</h2>

          {jobStatus ? (
            <>
              {/* Progress Bar */}
              <div className="mb-4">
                <div className="flex justify-between text-sm text-gray-600 mb-1">
                  <span>
                    {jobStatus.completed} / {jobStatus.total} domains processed
                  </span>
                  <span>
                    {jobStatus.successful} ok, {jobStatus.failed} failed
                  </span>
                </div>
                <div className="w-full bg-gray-200 rounded-full h-3">
                  <div
                    className="bg-blue-600 h-3 rounded-full transition-all duration-500"
                    style={{
                      width: `${jobStatus.total > 0 ? (jobStatus.completed / jobStatus.total) * 100 : 0}%`,
                    }}
                  />
                </div>
              </div>

              {/* Live Results */}
              {jobStatus.results?.length > 0 && (
                <div className="space-y-1">
                  {jobStatus.results.map((r, i) => (
                    <div key={i} className="text-sm py-1 flex items-center gap-2">
                      <span
                        className={
                          r.success ? "text-green-600" : "text-red-600"
                        }
                      >
                        {r.success ? "✓" : "✗"}
                      </span>
                      <span className="font-mono">{r.domain}</span>
                      {!r.success && r.error && (
                        <span className="text-red-500 text-xs truncate">
                          — {r.error}
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </>
          ) : (
            <div className="flex items-center gap-3 text-gray-500">
              <div className="animate-spin h-5 w-5 border-2 border-blue-600 border-t-transparent rounded-full" />
              <span>Starting removal job...</span>
            </div>
          )}
        </div>
      )}

      {/* Final Results */}
      {phase === "done" && removalResults.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          <h2 className="text-lg font-semibold mb-4">Removal Complete</h2>

          {/* Summary Cards */}
          <div className="grid grid-cols-3 gap-4 mb-6">
            <div className="bg-gray-50 rounded-lg p-4 text-center">
              <div className="text-2xl font-bold text-gray-900">
                {removalResults.length}
              </div>
              <div className="text-sm text-gray-500">Total</div>
            </div>
            <div className="bg-green-50 rounded-lg p-4 text-center">
              <div className="text-2xl font-bold text-green-600">
                {removalResults.filter((r) => r.success).length}
              </div>
              <div className="text-sm text-gray-500">Successful</div>
            </div>
            <div className="bg-red-50 rounded-lg p-4 text-center">
              <div className="text-2xl font-bold text-red-600">
                {removalResults.filter((r) => !r.success).length}
              </div>
              <div className="text-sm text-gray-500">Failed</div>
            </div>
          </div>

          {/* Detailed Results */}
          <div className="space-y-3">
            {removalResults.map((r, i) => (
              <div
                key={i}
                className={`p-4 rounded-lg border ${
                  r.success
                    ? "bg-green-50 border-green-200"
                    : "bg-red-50 border-red-200"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`text-lg ${
                      r.success ? "text-green-600" : "text-red-600"
                    }`}
                  >
                    {r.success ? "✓" : "✗"}
                  </span>
                  <span className="font-mono font-medium">{r.domain}</span>
                  <span className="text-xs text-gray-400">({r.source})</span>
                </div>

                {r.error && (
                  <p className="text-sm text-red-600 mt-1">❌ {r.error}</p>
                )}
                {r.warning && (
                  <p className="text-sm text-amber-600 mt-1">⚠️ {r.warning}</p>
                )}

                {/* Step-by-step results */}
                {r.steps && Object.keys(r.steps).length > 0 && (
                  <div className="mt-2 text-xs text-gray-500 space-y-1">
                    {Object.entries(r.steps).map(([step, d]) => {
                      const stepData = d as RemovalStepResult;
                      const icon = stepData.success
                        ? "✓"
                        : stepData.skipped
                          ? "⏭"
                          : "✗";
                      const color = stepData.success
                        ? "text-green-600"
                        : stepData.skipped
                          ? "text-gray-400"
                          : "text-red-600";

                      const stepLabel = step
                        .replace(/_/g, " ")
                        .replace(/\b\w/g, (c) => c.toUpperCase());

                      return (
                        <div key={step} className="flex items-center gap-1">
                          <span className={color}>{icon}</span>
                          <span className="font-medium">{stepLabel}:</span>
                          {stepData.removed !== undefined && (
                            <span>{stepData.removed} removed</span>
                          )}
                          {stepData.upns_reset !== undefined && (
                            <span>{stepData.upns_reset} reset</span>
                          )}
                          {stepData.records_deleted !== undefined && (
                            <span>
                              {stepData.records_deleted} DNS records deleted
                            </span>
                          )}
                          {stepData.mailboxes_archived !== undefined && (
                            <span>
                              {stepData.mailboxes_archived} mailboxes archived
                            </span>
                          )}
                          {stepData.note && (
                            <span className="text-gray-400">
                              ({stepData.note})
                            </span>
                          )}
                          {stepData.error && (
                            <span className="text-red-500">
                              — {stepData.error}
                            </span>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            ))}
          </div>

          {/* Reset Button */}
          <button
            onClick={resetAll}
            className="mt-6 px-4 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 text-sm font-medium"
          >
            Remove More Domains
          </button>
        </div>
      )}
    </div>
  );
}
