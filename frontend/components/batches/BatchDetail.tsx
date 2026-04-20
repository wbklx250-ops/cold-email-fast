"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface ReconciliationTenantResult {
  tenant_id?: string;
  tenant_name?: string;
  admin_email?: string;
  sd?: {
    success?: boolean;
    sd_disabled?: boolean;
    action?: string;
    error?: string | null;
    fallback_used?: boolean;
  };
  smtp?: {
    success?: boolean;
    smtp_auth_disabled?: boolean;
    action?: string;
    error?: string | null;
  };
  error?: string | null;
}

interface ReconciliationSummary {
  status: "idle" | "running" | "completed" | "error";
  batch_id?: string;
  started_at?: string;
  finished_at?: string;
  auto_fix?: boolean;
  total_tenants?: number;
  processed?: number;
  sd_ok?: number;
  sd_drift_fixed?: number;
  sd_drift_unfixable?: number;
  sd_fallback_used?: number;
  smtp_ok?: number;
  smtp_drift_fixed?: number;
  smtp_drift_unfixable?: number;
  errors?: number;
  results?: ReconciliationTenantResult[];
  message?: string;
}

interface BatchDetailProps {
  batchId: string;
}

export default function BatchDetail({ batchId }: BatchDetailProps) {
  const [summary, setSummary] = useState<ReconciliationSummary | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showDetails, setShowDetails] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchStatus = useCallback(async (): Promise<ReconciliationSummary | null> => {
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/reconciliation/batches/${batchId}/status`,
      );
      if (res.status === 404) {
        return null;
      }
      if (!res.ok) {
        return null;
      }
      const data: ReconciliationSummary = await res.json();
      setSummary(data);
      return data;
    } catch {
      return null;
    }
  }, [batchId]);

  const startPolling = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      const data = await fetchStatus();
      if (!data || data.status !== "running") {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }
    }, 3000);
  }, [fetchStatus]);

  // Fetch once on mount in case a prior job exists; pick up polling if it's running
  useEffect(() => {
    (async () => {
      const data = await fetchStatus();
      if (data && data.status === "running") {
        startPolling();
      }
    })();
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [fetchStatus, startPolling]);

  const handleVerifyClick = async () => {
    const confirmed = window.confirm(
      "Verify & repair SD + SMTP state for every tenant in this batch?\n\n" +
        "This uses Graph API + PowerShell only (no Selenium on the happy path). " +
        "A 300-tenant batch can take ~10 minutes.",
    );
    if (!confirmed) return;

    setError(null);
    setIsStarting(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/reconciliation/batches/${batchId}/verify?auto_fix=true`,
        { method: "POST" },
      );
      if (res.status === 409) {
        setError("A reconciliation job is already running for this batch.");
        // pick up existing job
        await fetchStatus();
        startPolling();
        return;
      }
      if (!res.ok) {
        const txt = await res.text();
        setError(`Failed to start reconciliation: ${txt}`);
        return;
      }
      // Optimistically show running state, then start polling
      setSummary({ status: "running", message: "Reconciliation started..." });
      startPolling();
    } catch (e) {
      setError(`Network error: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setIsStarting(false);
    }
  };

  const isRunning = summary?.status === "running";
  const isCompleted = summary?.status === "completed";
  const isErrored = summary?.status === "error";

  const statusBadgeClass = isRunning
    ? "bg-blue-50 text-blue-800 border-blue-200"
    : isCompleted
    ? "bg-green-50 text-green-800 border-green-200"
    : isErrored
    ? "bg-red-50 text-red-800 border-red-200"
    : "bg-gray-50 text-gray-700 border-gray-200";

  const statusLabel = isRunning
    ? "Running"
    : isCompleted
    ? "Completed"
    : isErrored
    ? "Error"
    : "Idle";

  const problemResults = (summary?.results || []).filter((r) => {
    if (r.error) return true;
    if (r.sd && r.sd.success === false) return true;
    if (r.smtp && r.smtp.success === false) return true;
    return false;
  });

  return (
    <div className="rounded-lg border border-gray-200 bg-white shadow-sm p-5 mb-6">
      <div className="flex flex-wrap items-start justify-between gap-3 mb-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-900">
            Verify &amp; Repair (Graph + PowerShell)
          </h2>
          <p className="text-xs text-gray-500 mt-1">
            Re-checks Security Defaults and SMTP Auth for every tenant in this batch.
            Uses Graph API + PowerShell only — Selenium is used strictly as a last-resort fallback.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`inline-block text-xs font-medium px-2 py-1 rounded border ${statusBadgeClass}`}
          >
            {statusLabel}
          </span>
          <button
            onClick={handleVerifyClick}
            disabled={isStarting || isRunning}
            className="px-4 py-2 text-sm font-semibold rounded-lg bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isRunning
              ? "Running..."
              : isStarting
              ? "Starting..."
              : "Verify & Repair"}
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-md bg-red-50 border border-red-200 text-red-800 text-xs px-3 py-2 mb-3">
          {error}
        </div>
      )}

      {summary && (isRunning || isCompleted || isErrored) && (
        <>
          {/* Progress line */}
          {isRunning && (
            <div className="text-xs text-blue-700 mb-3">
              Processed {summary.processed ?? 0} / {summary.total_tenants ?? 0} tenants...
            </div>
          )}

          {/* Summary grid */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <SummaryTile label="SD OK" value={summary.sd_ok} tone="green" />
            <SummaryTile
              label="SD Fixed"
              value={summary.sd_drift_fixed}
              tone="blue"
            />
            <SummaryTile
              label="SD Unfixable"
              value={summary.sd_drift_unfixable}
              tone="red"
            />
            <SummaryTile
              label="SD Fallback"
              value={summary.sd_fallback_used}
              tone="yellow"
            />
            <SummaryTile label="SMTP OK" value={summary.smtp_ok} tone="green" />
            <SummaryTile
              label="SMTP Fixed"
              value={summary.smtp_drift_fixed}
              tone="blue"
            />
            <SummaryTile
              label="SMTP Unfixable"
              value={summary.smtp_drift_unfixable}
              tone="red"
            />
            <SummaryTile label="Errors" value={summary.errors} tone="red" />
          </div>

          {/* Timing */}
          {(summary.started_at || summary.finished_at) && (
            <div className="mt-3 text-xs text-gray-500">
              {summary.started_at && (
                <span>
                  Started: {new Date(summary.started_at).toLocaleTimeString()}
                </span>
              )}
              {summary.finished_at && (
                <span className="ml-3">
                  Finished:{" "}
                  {new Date(summary.finished_at).toLocaleTimeString()}
                </span>
              )}
            </div>
          )}

          {/* Problems toggle */}
          {problemResults.length > 0 && (
            <div className="mt-4">
              <button
                onClick={() => setShowDetails((v) => !v)}
                className="text-xs text-indigo-600 hover:text-indigo-800 underline"
              >
                {showDetails
                  ? "Hide"
                  : `Show ${problemResults.length} tenant issue(s)`}
              </button>
              {showDetails && (
                <div className="mt-2 max-h-64 overflow-y-auto border border-gray-200 rounded-md">
                  <table className="min-w-full divide-y divide-gray-200 text-xs">
                    <thead className="bg-gray-50 sticky top-0">
                      <tr>
                        <th className="px-2 py-1 text-left font-medium text-gray-500 uppercase">
                          Tenant
                        </th>
                        <th className="px-2 py-1 text-left font-medium text-gray-500 uppercase">
                          SD
                        </th>
                        <th className="px-2 py-1 text-left font-medium text-gray-500 uppercase">
                          SMTP
                        </th>
                        <th className="px-2 py-1 text-left font-medium text-gray-500 uppercase">
                          Error
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {problemResults.map((r, i) => (
                        <tr key={r.tenant_id || i} className="hover:bg-gray-50">
                          <td
                            className="px-2 py-1 font-mono text-[11px] max-w-[220px] truncate"
                            title={r.tenant_name || r.admin_email || r.tenant_id}
                          >
                            {r.tenant_name || r.admin_email || r.tenant_id}
                          </td>
                          <td className="px-2 py-1">
                            {r.sd
                              ? `${r.sd.action || "?"}${
                                  r.sd.fallback_used ? " (selenium fb)" : ""
                                }`
                              : "-"}
                          </td>
                          <td className="px-2 py-1">
                            {r.smtp ? r.smtp.action || "?" : "-"}
                          </td>
                          <td
                            className="px-2 py-1 text-red-600 max-w-[260px] truncate"
                            title={
                              r.error ||
                              r.sd?.error ||
                              r.smtp?.error ||
                              ""
                            }
                          >
                            {r.error || r.sd?.error || r.smtp?.error || "-"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

interface SummaryTileProps {
  label: string;
  value?: number;
  tone: "green" | "blue" | "red" | "yellow" | "gray";
}

function SummaryTile({ label, value, tone }: SummaryTileProps) {
  const toneClass = {
    green: "bg-green-50 text-green-800 border-green-200",
    blue: "bg-blue-50 text-blue-800 border-blue-200",
    red: "bg-red-50 text-red-800 border-red-200",
    yellow: "bg-yellow-50 text-yellow-800 border-yellow-200",
    gray: "bg-gray-50 text-gray-700 border-gray-200",
  }[tone];
  return (
    <div className={`rounded-md border px-2 py-1.5 ${toneClass}`}>
      <div className="text-[10px] uppercase tracking-wide opacity-70">
        {label}
      </div>
      <div className="text-base font-semibold">{value ?? 0}</div>
    </div>
  );
}
