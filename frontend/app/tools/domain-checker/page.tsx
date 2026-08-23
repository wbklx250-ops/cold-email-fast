"use client";

import { useState, useEffect, useRef, useCallback, useMemo } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface DomainEntry {
  name: string;
  status: string;
}

interface TenantResult {
  admin_email: string;
  tenant_name: string;
  login_success: boolean;
  login_error: string;
  verified_domains: DomainEntry[];
  unverified_domains: DomainEntry[];
  verified_count: number;
  unverified_count: number;
  custom_domain_count: number;
}

interface JobSummary {
  auth_success: number;
  auth_failed: number;
  tenants_with_verified_domains: number;
  tenants_with_unverified_domains: number;
  tenants_no_domains: number;
  total_verified_domains: number;
  total_unverified_domains: number;
}

interface JobStatus {
  job_id: string;
  status: "running" | "complete" | "error";
  total: number;
  processed: number;
  results: TenantResult[];
  summary: JobSummary;
  started_at: string;
  completed_at: string | null;
}

interface BatchOption {
  id: string;
  name: string;
  tenant_count: number;
}

type TenantDisposition = "unreviewed" | "available" | "active" | "burned";

interface TenantAudit {
  id: string;
  admin_email: string;
  tenant_name: string;
  disposition: TenantDisposition;
  login_success: boolean;
  login_error: string | null;
  is_used: boolean | null;
  verified_domains: DomainEntry[];
  unverified_domains: DomainEntry[];
  custom_domain_count: number;
  last_checked_at: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getBrowserHelperText(count: number): { icon: string; text: string } {
  if (count === 1) return { icon: "🐢", text: "1 browser · Sequential · ~35-40 min for 100 tenants" };
  if (count === 2) return { icon: "⚡", text: "2 browsers · ~18-20 min for 100 tenants" };
  if (count === 3) return { icon: "⚡", text: "3 browsers · ~12-14 min for 100 tenants (recommended)" };
  if (count <= 5) return { icon: "🚀", text: `${count} browsers · ~8-10 min for 100 tenants · Needs 2GB+ RAM` };
  if (count <= 8) return { icon: "🚀", text: `${count} browsers · ~5-7 min for 100 tenants · Needs 4GB+ RAM` };
  return { icon: "⚠️", text: `${count} browsers · ~4-5 min for 100 tenants · Needs 8GB+ RAM` };
}

function formatDuration(startedAt: string, completedAt: string | null): string {
  if (!startedAt || !completedAt) return "";
  const start = new Date(startedAt + "Z");
  const end = new Date(completedAt + "Z");
  const diffMs = end.getTime() - start.getTime();
  if (diffMs < 0) return "";
  const totalSec = Math.floor(diffMs / 1000);
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  if (mins === 0) return `${secs}s`;
  return `${mins}m ${secs}s`;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function DomainCheckerPage() {
  // Input state
  const [mode, setMode] = useState<"csv" | "batch">("csv");
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [credentialsText, setCredentialsText] = useState("");
  const [batches, setBatches] = useState<BatchOption[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string>("");
  const [headless, setHeadless] = useState(true);
  const [parallelBrowsers, setParallelBrowsers] = useState(3);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Job state
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Persistent inventory state
  const [inventory, setInventory] = useState<TenantAudit[]>([]);
  const [inventoryLoading, setInventoryLoading] = useState(true);
  const [usageFilter, setUsageFilter] = useState<"all" | "used" | "unused" | "unknown">("all");
  const [inventorySearch, setInventorySearch] = useState("");

  // Drag state
  const [isDragging, setIsDragging] = useState(false);

  const loadInventory = useCallback(async () => {
    setInventoryLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/domain-checker/inventory`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setInventory(await res.json());
    } catch (err) {
      setError(`Could not load tenant inventory: ${(err as Error).message}`);
    } finally {
      setInventoryLoading(false);
    }
  }, []);

  // Fetch batches on mount
  useEffect(() => {
    loadInventory();
    fetch(`${API_BASE}/api/v1/wizard/batches`)
      .then((res) => res.json())
      .then((data) => {
        const batchList = (data || []).map((b: any) => ({
          id: b.id,
          name: b.name || b.id,
          tenant_count: b.tenant_count || b.tenants_count || 0,
        }));
        setBatches(batchList);
      })
      .catch(() => {});
  }, [loadInventory]);

  // Poll job status
  useEffect(() => {
    if (!jobId) return;

    const interval = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/v1/domain-checker/jobs/${jobId}`);
        if (!res.ok) return;
        const data: JobStatus = await res.json();
        setJobStatus(data);
        if (data.status === "complete" || data.status === "error") {
          clearInterval(interval);
          setLoading(false);
          if (data.status === "complete") loadInventory();
        }
      } catch {
        // ignore polling errors
      }
    }, 3000);

    return () => clearInterval(interval);
  }, [jobId, loadInventory]);

  // Start check from CSV
  const handleStartCSV = async () => {
    if (!csvFile && !credentialsText.trim()) return;
    setLoading(true);
    setError(null);
    setJobId(null);
    setJobStatus(null);

    try {
      const formData = new FormData();
      if (csvFile) formData.append("file", csvFile);
      if (!csvFile && credentialsText.trim()) {
        formData.append("credentials_text", credentialsText.trim());
      }
      formData.append("headless", String(headless));
      formData.append("max_workers", parallelBrowsers.toString());

      const res = await fetch(`${API_BASE}/api/v1/domain-checker/check-csv`, {
        method: "POST",
        body: formData,
      });

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      setJobId(data.job_id);
    } catch (err) {
      setError("Failed to start check: " + (err as Error).message);
      setLoading(false);
    }
  };

  // Start check from batch
  const handleStartBatch = async () => {
    if (!selectedBatchId) return;
    setLoading(true);
    setError(null);
    setJobId(null);
    setJobStatus(null);

    try {
      const formData = new FormData();
      formData.append("headless", String(headless));
      formData.append("max_workers", parallelBrowsers.toString());

      const res = await fetch(
        `${API_BASE}/api/v1/domain-checker/check-batch/${selectedBatchId}`,
        {
          method: "POST",
          body: formData,
        }
      );

      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }

      const data = await res.json();
      setJobId(data.job_id);
    } catch (err) {
      setError("Failed to start check: " + (err as Error).message);
      setLoading(false);
    }
  };

  // Drag & drop handlers
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const files = e.dataTransfer.files;
    if (files.length > 0 && files[0].name.endsWith(".csv")) {
      setCsvFile(files[0]);
      setMode("csv");
    }
  }, []);

  // Download CSV
  const handleDownloadCSV = () => {
    if (!jobId) return;
    window.open(`${API_BASE}/api/v1/domain-checker/jobs/${jobId}/csv`, "_blank");
  };

  const handleDispositionChange = async (auditId: string, disposition: TenantDisposition) => {
    const previous = inventory;
    setInventory((items) =>
      items.map((item) => (item.id === auditId ? { ...item, disposition } : item))
    );
    try {
      const res = await fetch(`${API_BASE}/api/v1/domain-checker/inventory/${auditId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ disposition }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      setInventory(previous);
      setError(`Could not update tenant: ${(err as Error).message}`);
    }
  };

  // Computed
  const isRunning = jobStatus?.status === "running";
  const isComplete = jobStatus?.status === "complete";
  const progress = jobStatus
    ? jobStatus.total > 0
      ? Math.round((jobStatus.processed / jobStatus.total) * 100)
      : 0
    : 0;

  const browserHelper = getBrowserHelperText(parallelBrowsers);

  const inventoryCounts = useMemo(
    () => ({
      total: inventory.length,
      used: inventory.filter((item) => item.is_used === true).length,
      unused: inventory.filter((item) => item.is_used === false).length,
      unknown: inventory.filter((item) => item.is_used === null).length,
      burned: inventory.filter((item) => item.disposition === "burned").length,
    }),
    [inventory]
  );

  const filteredInventory = useMemo(() => {
    const query = inventorySearch.trim().toLowerCase();
    return inventory.filter((item) => {
      const matchesUsage =
        usageFilter === "all" ||
        (usageFilter === "used" && item.is_used === true) ||
        (usageFilter === "unused" && item.is_used === false) ||
        (usageFilter === "unknown" && item.is_used === null);
      const domains = [...item.verified_domains, ...item.unverified_domains]
        .map((domain) => domain.name)
        .join(" ");
      const matchesSearch =
        !query ||
        `${item.tenant_name} ${item.admin_email} ${domains}`.toLowerCase().includes(query);
      return matchesUsage && matchesSearch;
    });
  }, [inventory, inventorySearch, usageFilter]);

  // Build completion readout data
  const completionData = useMemo(() => {
    if (!jobStatus || jobStatus.status !== "complete") return null;

    const verified: Record<string, string[]> = {};
    const unverified: Record<string, string[]> = {};
    const loginFailed: { tenant: string; error: string }[] = [];
    const noDomains: string[] = [];

    for (const r of jobStatus.results) {
      if (!r.login_success) {
        loginFailed.push({ tenant: r.tenant_name, error: r.login_error });
        continue;
      }

      if (r.verified_domains?.length > 0) {
        verified[r.tenant_name] = r.verified_domains.map((d) => d.name);
      }

      if (r.unverified_domains?.length > 0) {
        unverified[r.tenant_name] = r.unverified_domains.map((d) => d.name);
      }

      if (
        (r.verified_domains?.length || 0) === 0 &&
        (r.unverified_domains?.length || 0) === 0
      ) {
        noDomains.push(r.tenant_name);
      }
    }

    return { verified, unverified, loginFailed, noDomains };
  }, [jobStatus]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Tenant Inventory</h1>
        <p className="text-sm text-gray-500 mt-1">
          Audit tenant logins, discover their M365 domains, and track whether each
          tenant is active, available, or burned
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

      {/* Persistent inventory */}
      <section className="bg-white rounded-lg border border-gray-200 overflow-hidden">
        <div className="p-5 border-b border-gray-200">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="font-semibold text-gray-900">Checked tenants</h2>
              <p className="text-xs text-gray-500 mt-0.5">
                Usage comes from discovered custom domains. Status is managed by you.
              </p>
            </div>
            <button
              onClick={loadInventory}
              disabled={inventoryLoading}
              className="px-3 py-2 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {inventoryLoading ? "Refreshing…" : "↻ Refresh"}
            </button>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 mt-4">
            {([
              ["all", "Total", inventoryCounts.total, "text-gray-900"],
              ["used", "Used", inventoryCounts.used, "text-emerald-700"],
              ["unused", "Unused", inventoryCounts.unused, "text-blue-700"],
              ["unknown", "Unknown", inventoryCounts.unknown, "text-amber-700"],
            ] as const).map(([filter, label, value, color]) => (
              <button
                key={filter}
                onClick={() => setUsageFilter(filter)}
                className={`text-left rounded-lg border p-3 transition-colors ${
                  usageFilter === filter
                    ? "border-blue-400 bg-blue-50"
                    : "border-gray-200 hover:bg-gray-50"
                }`}
              >
                <div className={`text-xl font-bold ${color}`}>{value}</div>
                <div className="text-xs text-gray-500">{label}</div>
              </button>
            ))}
            <div className="rounded-lg border border-red-200 bg-red-50 p-3">
              <div className="text-xl font-bold text-red-700">{inventoryCounts.burned}</div>
              <div className="text-xs text-red-600">Burned</div>
            </div>
          </div>

          <input
            value={inventorySearch}
            onChange={(event) => setInventorySearch(event.target.value)}
            placeholder="Search tenant, email, or domain…"
            className="mt-4 w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>

        {inventoryLoading && inventory.length === 0 ? (
          <div className="p-8 text-center text-sm text-gray-500">Loading inventory…</div>
        ) : filteredInventory.length === 0 ? (
          <div className="p-8 text-center text-sm text-gray-500">
            {inventory.length === 0
              ? "No tenants checked yet. Paste credentials or upload a CSV below to begin."
              : "No tenants match this filter."}
          </div>
        ) : (
          <div className="overflow-x-auto max-h-[32rem] overflow-y-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Tenant</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Usage</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Domains</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Last checked</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {filteredInventory.map((item) => {
                  const domains = [...item.verified_domains, ...item.unverified_domains];
                  return (
                    <tr key={item.id} className={item.disposition === "burned" ? "bg-red-50/50" : ""}>
                      <td className="px-4 py-3 text-sm">
                        <div className="font-medium text-gray-900">{item.tenant_name}</div>
                        <div className="text-xs text-gray-500 font-mono">{item.admin_email}</div>
                        {!item.login_success && item.login_error && (
                          <div className="text-xs text-red-600 mt-1 max-w-xs truncate" title={item.login_error}>
                            Login failed: {item.login_error}
                          </div>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm whitespace-nowrap">
                        {item.is_used === true ? (
                          <span className="px-2 py-1 rounded-full bg-emerald-100 text-emerald-700 text-xs font-medium">Used</span>
                        ) : item.is_used === false ? (
                          <span className="px-2 py-1 rounded-full bg-blue-100 text-blue-700 text-xs font-medium">Unused</span>
                        ) : (
                          <span className="px-2 py-1 rounded-full bg-amber-100 text-amber-700 text-xs font-medium">Unknown</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-xs font-mono text-gray-700 min-w-60">
                        {domains.length > 0 ? (
                          <div className="space-y-1">
                            {domains.map((domain, index) => (
                              <div key={`${domain.name}-${index}`}>{domain.name}</div>
                            ))}
                          </div>
                        ) : (
                          <span className="text-gray-400">No custom domains</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <select
                          value={item.disposition}
                          onChange={(event) =>
                            handleDispositionChange(item.id, event.target.value as TenantDisposition)
                          }
                          className={`border rounded-lg px-2 py-1.5 text-sm font-medium ${
                            item.disposition === "burned"
                              ? "border-red-300 bg-red-50 text-red-700"
                              : "border-gray-300 bg-white text-gray-700"
                          }`}
                        >
                          <option value="unreviewed">Unreviewed</option>
                          <option value="available">Available</option>
                          <option value="active">Active</option>
                          <option value="burned">Burned</option>
                        </select>
                      </td>
                      <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">
                        {new Date(item.last_checked_at).toLocaleString()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Input Section */}
      {!isRunning && !isComplete && (
        <div className="bg-white rounded-lg border border-gray-200 p-6">
          {/* Mode tabs */}
          <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit mb-6">
            <button
              onClick={() => setMode("csv")}
              className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                mode === "csv"
                  ? "bg-white shadow text-gray-900"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              📄 Upload CSV
            </button>
            <button
              onClick={() => setMode("batch")}
              className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${
                mode === "batch"
                  ? "bg-white shadow text-gray-900"
                  : "text-gray-500 hover:text-gray-700"
              }`}
            >
              📋 Select Batch
            </button>
          </div>

          {/* CSV upload */}
          {mode === "csv" && (
            <div>
              <div
                onDragOver={handleDragOver}
                onDragLeave={handleDragLeave}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
                className={`border-2 border-dashed rounded-lg p-8 text-center cursor-pointer transition-colors ${
                  isDragging
                    ? "border-blue-400 bg-blue-50"
                    : csvFile
                      ? "border-green-300 bg-green-50"
                      : "border-gray-300 hover:border-gray-400 hover:bg-gray-50"
                }`}
              >
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".csv"
                  className="hidden"
                  onChange={(e) => {
                    if (e.target.files?.[0]) setCsvFile(e.target.files[0]);
                  }}
                />
                {csvFile ? (
                  <div>
                    <span className="text-2xl">✅</span>
                    <p className="mt-2 text-sm font-medium text-green-700">
                      {csvFile.name}
                    </p>
                    <p className="text-xs text-gray-500 mt-1">
                      Click or drag to replace
                    </p>
                  </div>
                ) : (
                  <div>
                    <span className="text-3xl">📁</span>
                    <p className="mt-2 text-sm font-medium text-gray-700">
                      Drop a CSV file here, or click to browse
                    </p>
                    <p className="text-xs text-gray-500 mt-1">
                      CSV should contain email + password columns
                    </p>
                  </div>
                )}
              </div>
              <div className="flex items-center gap-3 my-4">
                <div className="h-px bg-gray-200 flex-1" />
                <span className="text-xs font-medium text-gray-400 uppercase">or paste credentials</span>
                <div className="h-px bg-gray-200 flex-1" />
              </div>
              <textarea
                value={credentialsText}
                onChange={(event) => {
                  setCredentialsText(event.target.value);
                  if (event.target.value) setCsvFile(null);
                }}
                rows={6}
                spellCheck={false}
                placeholder={"admin@tenant.onmicrosoft.com,password,optional-totp\nadmin@another.onmicrosoft.com,password"}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
              <p className="text-xs text-gray-500 mt-1.5">
                One tenant per line. Comma, tab, and pipe separators are supported. Passwords and TOTP secrets are used for this check only and are not saved in the inventory.
              </p>
            </div>
          )}

          {/* Batch selector */}
          {mode === "batch" && (
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Select a batch
              </label>
              <select
                value={selectedBatchId}
                onChange={(e) => setSelectedBatchId(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2.5 text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              >
                <option value="">— Choose a batch —</option>
                {batches.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name} ({b.tenant_count} tenants)
                  </option>
                ))}
              </select>
              {batches.length === 0 && (
                <p className="mt-2 text-xs text-gray-400">
                  No batches found. Create a batch first or upload a CSV.
                </p>
              )}
            </div>
          )}

          {/* Parallel Browsers Slider */}
          <div className="mt-5">
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Parallel Browsers:{" "}
              <span className="text-blue-600 font-bold">{parallelBrowsers}</span>
            </label>
            <input
              type="range"
              min={1}
              max={10}
              value={parallelBrowsers}
              onChange={(e) => setParallelBrowsers(Number(e.target.value))}
              className="w-full h-2 bg-gray-200 rounded-lg appearance-none cursor-pointer accent-blue-600"
            />
            <div className="flex justify-between text-xs text-gray-400 mt-1 px-0.5">
              <span>1</span>
              <span>5</span>
              <span>10</span>
            </div>
            <p className="text-xs text-gray-500 mt-1.5">
              {browserHelper.icon} {browserHelper.text}
            </p>
          </div>

          {/* Options */}
          <div className="mt-4 flex items-center gap-4">
            <label className="flex items-center gap-2 text-sm text-gray-600">
              <input
                type="checkbox"
                checked={headless}
                onChange={(e) => setHeadless(e.target.checked)}
                className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              Headless mode (no visible browser)
            </label>
          </div>

          {/* Start button */}
          <div className="mt-6">
            <button
              onClick={mode === "csv" ? handleStartCSV : handleStartBatch}
              disabled={
                loading ||
                (mode === "csv" && !csvFile && !credentialsText.trim()) ||
                (mode === "batch" && !selectedBatchId)
              }
              className="px-6 py-2.5 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed text-sm font-medium"
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <span className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full" />
                  Starting...
                </span>
              ) : (
                "🔍 Start Domain Check"
              )}
            </button>
          </div>
        </div>
      )}

      {/* Progress Bar */}
      {jobStatus && (isRunning || isComplete) && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-gray-700">
              {isRunning ? "Checking tenants..." : "Check complete"}
            </span>
            <span className="text-sm text-gray-500">
              {jobStatus.processed}/{jobStatus.total} ({progress}%)
            </span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-3">
            <div
              className={`h-3 rounded-full transition-all duration-500 ${
                isComplete ? "bg-green-500" : "bg-blue-500"
              }`}
              style={{ width: `${progress}%` }}
            />
          </div>
          {isRunning && jobStatus.processed > 0 && (
            <p className="text-xs text-gray-400 mt-2">
              Currently processing tenant {jobStatus.processed + 1} of{" "}
              {jobStatus.total}...
            </p>
          )}
        </div>
      )}

      {/* Summary Cards */}
      {jobStatus && jobStatus.summary && Object.keys(jobStatus.summary).length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
          <div className="bg-white rounded-lg border border-gray-200 p-4 text-center">
            <div className="text-2xl font-bold text-gray-900">
              {jobStatus.total}
            </div>
            <div className="text-sm text-gray-500">Total Tenants</div>
          </div>
          <div className="bg-green-50 rounded-lg border border-green-200 p-4 text-center">
            <div className="text-2xl font-bold text-green-600">
              {jobStatus.summary.auth_success || 0}
            </div>
            <div className="text-sm text-gray-500">Auth OK</div>
          </div>
          <div className="bg-red-50 rounded-lg border border-red-200 p-4 text-center">
            <div className="text-2xl font-bold text-red-600">
              {jobStatus.summary.auth_failed || 0}
            </div>
            <div className="text-sm text-gray-500">Auth Failed</div>
          </div>
          <div className="bg-emerald-50 rounded-lg border border-emerald-200 p-4 text-center">
            <div className="text-2xl font-bold text-emerald-600">
              {jobStatus.summary.total_verified_domains || 0}
            </div>
            <div className="text-sm text-gray-500">Verified Domains</div>
          </div>
          <div className="bg-amber-50 rounded-lg border border-amber-200 p-4 text-center">
            <div className="text-2xl font-bold text-amber-600">
              {jobStatus.summary.total_unverified_domains || 0}
            </div>
            <div className="text-sm text-gray-500">Unverified Domains</div>
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* Completion Summary Panel */}
      {/* ============================================================== */}
      {isComplete && completionData && jobStatus && (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          {/* Header */}
          <div className="bg-green-50 border-b border-green-200 px-6 py-4 flex items-center justify-between">
            <div>
              <h2 className="text-lg font-semibold text-green-800">
                ✅ Domain Check Complete
              </h2>
              <p className="text-sm text-green-700 mt-0.5">
                Checked {jobStatus.total} tenants
                {jobStatus.started_at && jobStatus.completed_at
                  ? ` in ${formatDuration(jobStatus.started_at, jobStatus.completed_at)}`
                  : ""}
              </p>
            </div>
            <button
              onClick={handleDownloadCSV}
              className="px-4 py-2 bg-white border border-green-300 text-green-700 rounded-lg hover:bg-green-50 text-sm font-medium shadow-sm"
            >
              📥 Download CSV
            </button>
          </div>

          <div className="p-6 space-y-6">
            {/* Two-column domain lists */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Verified Domains */}
              <div>
                <h3 className="text-sm font-semibold text-emerald-700 uppercase tracking-wider mb-3">
                  Verified Domains ({jobStatus.summary.total_verified_domains || 0})
                </h3>
                <div className="bg-emerald-50 rounded-lg border border-emerald-200 max-h-64 overflow-y-auto">
                  {Object.keys(completionData.verified).length > 0 ? (
                    <div className="p-3 space-y-3">
                      {Object.entries(completionData.verified).map(
                        ([tenant, domains]) => (
                          <div key={tenant}>
                            <p className="text-xs font-semibold text-emerald-800 mb-1">
                              {tenant}:
                            </p>
                            {domains.map((d, i) => (
                              <p
                                key={i}
                                className="text-sm text-emerald-700 pl-3"
                              >
                                ✅ {d}
                              </p>
                            ))}
                          </div>
                        )
                      )}
                    </div>
                  ) : (
                    <p className="p-3 text-sm text-gray-400 italic">
                      No verified domains found
                    </p>
                  )}
                </div>
              </div>

              {/* Unverified Domains */}
              <div>
                <h3 className="text-sm font-semibold text-amber-700 uppercase tracking-wider mb-3">
                  Unverified Domains ({jobStatus.summary.total_unverified_domains || 0})
                </h3>
                <div className="bg-amber-50 rounded-lg border border-amber-200 max-h-64 overflow-y-auto">
                  {Object.keys(completionData.unverified).length > 0 ? (
                    <div className="p-3 space-y-3">
                      {Object.entries(completionData.unverified).map(
                        ([tenant, domains]) => (
                          <div key={tenant}>
                            <p className="text-xs font-semibold text-amber-800 mb-1">
                              {tenant}:
                            </p>
                            {domains.map((d, i) => (
                              <p
                                key={i}
                                className="text-sm text-amber-700 pl-3"
                              >
                                ⚠️ {d}
                              </p>
                            ))}
                          </div>
                        )
                      )}
                    </div>
                  ) : (
                    <p className="p-3 text-sm text-gray-400 italic">
                      No unverified domains found
                    </p>
                  )}
                </div>
              </div>
            </div>

            {/* Login Failures */}
            {completionData.loginFailed.length > 0 && (
              <div>
                <h3 className="text-sm font-semibold text-red-700 uppercase tracking-wider mb-3">
                  Login Failures ({completionData.loginFailed.length})
                </h3>
                <div className="bg-red-50 rounded-lg border border-red-200 max-h-48 overflow-y-auto">
                  <div className="p-3 space-y-1.5">
                    {completionData.loginFailed.map((f, i) => (
                      <p key={i} className="text-sm text-red-700">
                        <span className="font-medium">{f.tenant}</span>
                        <span className="text-red-500">
                          {" "}
                          — {f.error || "Login failed"}
                        </span>
                      </p>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {/* No Custom Domains */}
            {completionData.noDomains.length > 0 && (
              <div>
                <p className="text-sm text-gray-500">
                  <span className="font-medium text-gray-700">
                    No custom domains ({completionData.noDomains.length} tenants):
                  </span>{" "}
                  {completionData.noDomains.join(", ")}
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ============================================================== */}
      {/* Results Table */}
      {/* ============================================================== */}
      {jobStatus && jobStatus.results.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Tenant
                  </th>
                  <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase tracking-wider">
                    Login
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-emerald-600 uppercase tracking-wider">
                    Verified Domains
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-amber-600 uppercase tracking-wider">
                    Unverified Domains
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {jobStatus.results.map((r, i) => (
                  <tr
                    key={i}
                    className={
                      !r.login_success
                        ? "bg-red-50/50"
                        : r.custom_domain_count > 0
                          ? "bg-green-50/30"
                          : ""
                    }
                  >
                    <td className="px-4 py-3 text-sm whitespace-nowrap">
                      <div className="font-medium text-gray-900">
                        {r.tenant_name}
                      </div>
                      <div className="text-xs text-gray-400 font-mono">
                        {r.admin_email}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-center whitespace-nowrap">
                      {r.login_success ? (
                        <span className="text-green-600" title="Login successful">
                          ✅
                        </span>
                      ) : (
                        <span
                          className="text-red-500 cursor-help"
                          title={r.login_error || "Login failed"}
                        >
                          ❌
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm align-top">
                      {!r.login_success ? (
                        <span className="text-gray-400 text-xs">
                          — Login failed
                        </span>
                      ) : r.verified_domains && r.verified_domains.length > 0 ? (
                        <div className="space-y-0.5">
                          {r.verified_domains.map((d, di) => (
                            <div key={di} className="text-emerald-700 text-xs font-mono">
                              ✅ {d.name}
                            </div>
                          ))}
                        </div>
                      ) : r.custom_domain_count === 0 ? (
                        <span className="text-gray-400 text-xs italic">
                          (no custom domains)
                        </span>
                      ) : (
                        <span className="text-gray-400">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm align-top">
                      {!r.login_success ? (
                        <span className="text-gray-400 text-xs">
                          — Login failed
                        </span>
                      ) : r.unverified_domains &&
                        r.unverified_domains.length > 0 ? (
                        <div className="space-y-0.5">
                          {r.unverified_domains.map((d, di) => (
                            <div key={di} className="text-amber-700 text-xs font-mono">
                              ⚠️ {d.name}
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span className="text-gray-400">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Table Footer */}
          <div className="bg-gray-50 px-4 py-3 border-t border-gray-200 flex items-center justify-between">
            <span className="text-sm text-gray-500">
              {jobStatus.results.length} of {jobStatus.total} tenants processed
            </span>
            <div className="flex gap-3">
              {isComplete && (
                <button
                  onClick={() => {
                    setJobId(null);
                    setJobStatus(null);
                    setCsvFile(null);
                    setSelectedBatchId("");
                  }}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium"
                >
                  🔄 New Check
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
