"use client";

import { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import BatchDetail from "@/components/batches/BatchDetail";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const STEP_NAMES: Record<number, string> = {
  1: "Create Cloudflare Zones",
  2: "Update Nameservers",
  3: "Verify NS Propagation",
  4: "Create DNS Records",
  5: "First Login",
  6: "M365 Domain Setup",
  7: "Create Mailboxes",
  8: "Enable SMTP Auth",
  9: "Export Credentials",
  10: "Upload to Sequencer",
  11: "Reconciliation & Verification",
};

interface PipelineStatus {
  status: string;
  batch_id: string;
  batch_name: string;
  current_step: number;
  current_step_name: string;
  message: string;
  total_domains: number;
  total_tenants: number;
  domains_per_tenant?: number;
  nameserver_groups?: Array<{ nameservers: string[]; domains: string[]; count: number }>;
  steps: Record<string, { status: string; completed: number; failed: number; total: number }>;
  errors: Array<{ step: number; error: string }>;
  activity_log: Array<{ step: number; step_name: string; item_name: string; status: string; message: string; timestamp: string }>;
  completed_at?: string;
}

interface LogEntry {
  step: number;
  step_name: string;
  item_type: string;
  item_name: string;
  status: string;
  message: string;
  error: string | null;
  timestamp: string;
}

interface FailedDomain {
  id: string;
  name: string;
  error: string | null;
  retry_count: number;
  domain_added: boolean;
  domain_verified: boolean;
  dkim_enabled: boolean;
}

interface FailedDomainsData {
  step: number;
  step_name: string;
  failed: FailedDomain[];
  succeeded: Array<{ id: string; name: string }>;
  skipped: Array<{ id: string; name: string; error: string | null }>;
  summary: {
    failed_count: number;
    succeeded_count: number;
    skipped_count: number;
    total: number;
  };
}

interface SkipResult {
  success: boolean;
  skipped?: string[];
  skipped_count?: number;
  not_found?: string[];
  already_done?: string[];
  message: string;
}

export default function PipelineDashboard() {
  const params = useParams();
  const batchId = params.batchId as string;

  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatus | null>(null);
  const [activityLog, setActivityLog] = useState<LogEntry[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  // Domain Status Panel state
  const [failedDomains, setFailedDomains] = useState<FailedDomainsData | null>(null);
  const [selectedDomains, setSelectedDomains] = useState<Set<string>>(new Set());
  const [pasteList, setPasteList] = useState("");
  const [skipReason, setSkipReason] = useState("Cannot be released from old tenant");
  const [isSkipping, setIsSkipping] = useState(false);
  const [skipResult, setSkipResult] = useState<SkipResult | null>(null);
  const [showDomainPanel, setShowDomainPanel] = useState(false);

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/status`);
      if (res.ok) {
        const data = await res.json();
        setPipelineStatus(data);
      }
    } catch {
      console.error("Failed to fetch status");
    }
    setIsLoading(false);
  }, [batchId]);

  const fetchActivityLog = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/activity-log?limit=20`);
      if (res.ok) {
        const data = await res.json();
        setActivityLog(data.logs || []);
      }
    } catch {
      console.error("Failed to fetch activity log");
    }
  }, [batchId]);

  const fetchFailedDomains = useCallback(async (step: number) => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/failed-domains?step=${step}`);
      if (res.ok) {
        const data = await res.json();
        setFailedDomains(data);
        setShowDomainPanel(true);
      }
    } catch {
      console.error("Failed to fetch domain status");
    }
  }, [batchId]);

  // Poll for status every 3 seconds while running
  useEffect(() => {
    fetchStatus();
    fetchActivityLog();

    const interval = setInterval(() => {
      fetchStatus();
      fetchActivityLog();
    }, 3000);

    return () => clearInterval(interval);
  }, [fetchStatus, fetchActivityLog]);

  // Auto-fetch failed domains when pipeline is stuck on Step 6 or 7
  useEffect(() => {
    if (pipelineStatus && (pipelineStatus.status === "error" || pipelineStatus.status === "paused")) {
      const currentStep = pipelineStatus.current_step;
      if ((currentStep === 6 || currentStep === 7) && !failedDomains) {
        fetchFailedDomains(currentStep);
      }
    }
  }, [pipelineStatus, failedDomains, fetchFailedDomains]);

  const confirmNameservers = async () => {
    await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/confirm-nameservers`, { method: "POST" });
    fetchStatus();
  };

  const pausePipeline = async () => {
    await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/pause`, { method: "POST" });
    fetchStatus();
  };

  const resumePipeline = async () => {
    await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/resume`, { method: "POST" });
    fetchStatus();
  };

  const retryFailed = async () => {
    await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/retry-failed`, { method: "POST" });
    fetchStatus();
  };

  const downloadCredentials = () => {
    window.open(`${API_BASE}/api/v1/pipeline/${batchId}/credentials-export`, "_blank");
  };

  const skipDomains = async (domainNames?: string[], skipAll?: boolean) => {
    setIsSkipping(true);
    setSkipResult(null);
    try {
      // Combine selected checkboxes + pasted list
      const allDomains = new Set<string>(domainNames || []);

      // Add domains from paste textarea
      if (pasteList.trim()) {
        pasteList.split(/[\n,]+/).forEach((d) => {
          const clean = d.trim().toLowerCase();
          if (clean) allDomains.add(clean);
        });
      }

      // Add checkbox-selected domains
      selectedDomains.forEach((name) => allDomains.add(name));

      const step = pipelineStatus?.current_step || 6;

      const res = await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/skip-domains?step=${step}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          domain_names: skipAll ? null : Array.from(allDomains),
          skip_all_failed: skipAll || false,
          reason: skipReason,
        }),
      });

      const data = await res.json();
      setSkipResult(data);

      // Refresh the failed domains list
      await fetchFailedDomains(step);
      // Refresh pipeline status
      await fetchStatus();

      // Clear selections
      setSelectedDomains(new Set());
      setPasteList("");
    } catch {
      setSkipResult({ success: false, message: "Network error" });
    } finally {
      setIsSkipping(false);
    }
  };

  if (isLoading) {
    return <div className="max-w-4xl mx-auto py-12 text-center text-gray-500">Loading pipeline...</div>;
  }

  if (!pipelineStatus) {
    return <div className="max-w-4xl mx-auto py-12 text-center text-red-500">Pipeline not found</div>;
  }

  const isRunning = pipelineStatus.status === "running";
  const isPaused = pipelineStatus.status === "paused";
  const isComplete = pipelineStatus.status === "completed";
  const isError = pipelineStatus.status === "error";
  const waitingForNS = pipelineStatus.current_step === 2 && isPaused;
  const failedSteps = pipelineStatus.errors || [];

  // Calculate skip button count
  const pasteCount = pasteList.trim() ? pasteList.split(/[\n,]+/).filter(Boolean).length : 0;
  const skipButtonCount = selectedDomains.size + pasteCount;

  return (
    <div className="max-w-4xl mx-auto py-8 px-4">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{pipelineStatus.batch_name}</h1>
          <p className="text-sm text-gray-500">
            {pipelineStatus.total_domains} domains · {pipelineStatus.total_tenants} tenants
            {(pipelineStatus.domains_per_tenant ?? 1) > 1 && (
              <span className="ml-1 text-blue-600 font-medium">
                · {pipelineStatus.domains_per_tenant} domains/tenant
              </span>
            )}
          </p>
        </div>
        <div className="flex gap-2">
          {isRunning && (
            <button onClick={pausePipeline} className="px-4 py-2 text-sm rounded-lg border border-gray-300 hover:bg-gray-50">
              Pause
            </button>
          )}
          {isPaused && !waitingForNS && (
            <button onClick={resumePipeline} className="px-4 py-2 text-sm rounded-lg bg-blue-600 text-white hover:bg-blue-700">
              Resume
            </button>
          )}
          {(isComplete || isError) && (
            <button onClick={retryFailed} className="px-4 py-2 text-sm rounded-lg bg-orange-500 text-white hover:bg-orange-600">
              Retry Failed
            </button>
          )}
          {isComplete && (
            <button onClick={downloadCredentials} className="px-4 py-2 text-sm rounded-lg bg-green-600 text-white hover:bg-green-700">
              Download Credentials
            </button>
          )}
        </div>
      </div>

      {/* Resume Button — shows when pipeline is stuck/crashed/paused */}
      {pipelineStatus && (
        pipelineStatus.status === "error" ||
        pipelineStatus.status === "paused" ||
        pipelineStatus.status === "unknown"
      ) && (
        <button
          onClick={async () => {
            try {
              const res = await fetch(`${API_BASE}/api/v1/pipeline/${batchId}/resume`, { method: "POST" });
              const data = await res.json();
              alert(data.message || "Pipeline resumed!");
              fetchStatus();
            } catch (e) {
              alert("Failed to resume pipeline");
            }
          }}
          className="w-full rounded-lg bg-blue-600 py-3 text-sm font-semibold text-white hover:bg-blue-700 mb-4"
        >
          ▶ Resume Pipeline from Step {pipelineStatus.current_step}
        </button>
      )}

      {/* Overall Status Banner */}
      <div className={`rounded-lg p-4 mb-6 text-sm font-medium ${
        isComplete ? "bg-green-50 text-green-800 border border-green-200" :
        isError ? "bg-red-50 text-red-800 border border-red-200" :
        isPaused ? "bg-yellow-50 text-yellow-800 border border-yellow-200" :
        "bg-blue-50 text-blue-800 border border-blue-200"
      }`}>
        {pipelineStatus.message}
      </div>

      {/* NS Update Prompt (only shows at Step 2) */}
      {waitingForNS && pipelineStatus.nameserver_groups && (
        <div className="rounded-lg border-2 border-yellow-400 bg-yellow-50 p-6 mb-6">
          <h2 className="text-lg font-bold text-yellow-900 mb-3">⏸ Update Nameservers at Porkbun</h2>
          <p className="text-sm text-yellow-800 mb-4">
            Update the nameservers for all domains below, then click confirm.
          </p>

          {pipelineStatus.nameserver_groups.map((group, i) => (
            <div key={i} className="bg-white rounded-lg p-4 mb-3 border border-yellow-200">
              <p className="text-xs text-gray-500 mb-1">{group.count} domain(s):</p>
              <div className="flex gap-2 mb-2">
                {group.nameservers.map((ns) => (
                  <code
                    key={ns}
                    className="bg-gray-100 px-3 py-1 rounded text-sm font-mono cursor-pointer hover:bg-gray-200"
                    onClick={() => navigator.clipboard.writeText(ns)}
                    title="Click to copy"
                  >
                    {ns}
                  </code>
                ))}
              </div>
              <p className="text-xs text-gray-400">
                {group.domains.slice(0, 5).join(", ")}
                {group.domains.length > 5 && ` + ${group.domains.length - 5} more`}
              </p>
            </div>
          ))}

          <button
            onClick={confirmNameservers}
            className="mt-4 w-full rounded-lg bg-yellow-600 py-3 text-sm font-semibold text-white hover:bg-yellow-700"
          >
            ✓ I&apos;ve Updated All Nameservers — Continue
          </button>
        </div>
      )}

      {/* Step Progress */}
      <div className="space-y-2 mb-8">
        {Object.entries(STEP_NAMES).map(([stepNum, stepName]) => {
          const step = Number(stepNum);
          const stepData = pipelineStatus.steps?.[stepNum];
          const isCurrent = pipelineStatus.current_step === step;
          const isStepComplete = stepData?.status === "completed";
          const isStepError = stepData?.status === "error";
          const isStepRunning = stepData?.status === "running";
          const isStepSkipped = stepData?.status === "skipped";

          return (
            <div
              key={step}
              className={`flex items-center justify-between rounded-lg px-4 py-3 text-sm ${
                isStepComplete ? "bg-green-50 border border-green-200" :
                isStepError ? "bg-red-50 border border-red-200" :
                isStepRunning || isCurrent ? "bg-blue-50 border border-blue-300" :
                isStepSkipped ? "bg-gray-50 border border-gray-200" :
                "bg-gray-50 border border-gray-100"
              }`}
            >
              <div className="flex items-center gap-3">
                <span className="text-base">
                  {isStepComplete ? "✅" :
                   isStepError ? "❌" :
                   isStepRunning || isCurrent ? "🔄" :
                   isStepSkipped ? "⏭️" :
                   "⬜"}
                </span>
                <span className={isCurrent ? "font-semibold" : ""}>
                  Step {step}: {stepName}
                </span>
              </div>
              <div className="flex items-center gap-3">
                {stepData && (stepData.completed > 0 || stepData.failed > 0) && (
                  <span className="text-xs text-gray-500">
                    {stepData.completed} done
                    {stepData.failed > 0 && <span className="text-red-500"> · {stepData.failed} failed</span>}
                  </span>
                )}
                {/* View Domains button for Step 6/7 when in error or running */}
                {(isStepError || isStepRunning || (isCurrent && (isError || isPaused))) && (step === 6 || step === 7) && (
                  <button
                    onClick={() => fetchFailedDomains(step)}
                    className="text-xs text-blue-600 hover:text-blue-800 underline"
                  >
                    View Domains
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Verify & Repair — Graph + PowerShell reconciliation (Step 11) */}
      <BatchDetail batchId={batchId} />

      {/* Domain Status Panel — shows when pipeline is stuck or user clicks "View Domains" */}
      {(showDomainPanel || isError || isPaused) && failedDomains && (
        <div className="bg-white rounded-lg border shadow-sm p-6 mb-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900">
              Domain Status — {failedDomains.step_name}
            </h3>
            <button
              onClick={() => {
                setShowDomainPanel(false);
                setFailedDomains(null);
              }}
              className="text-gray-400 hover:text-gray-600 text-sm"
            >
              ✕ Close
            </button>
          </div>

          {/* Summary bar */}
          <div className="flex gap-4 mb-4 text-sm">
            <span className="text-green-600 font-medium">
              ✅ {failedDomains.summary.succeeded_count} succeeded
            </span>
            <span className="text-red-600 font-medium">
              ❌ {failedDomains.summary.failed_count} failed
            </span>
            <span className="text-gray-500 font-medium">
              ⏭️ {failedDomains.summary.skipped_count} skipped
            </span>
            <span className="text-gray-400">
              ({failedDomains.summary.total} total)
            </span>
          </div>

          {/* Skip result message */}
          {skipResult && (
            <div className={`mb-4 p-3 rounded-lg text-sm ${
              skipResult.success ? "bg-green-50 text-green-800 border border-green-200" : "bg-red-50 text-red-800 border border-red-200"
            }`}>
              <p className="font-medium">{skipResult.message}</p>
              {skipResult.not_found && skipResult.not_found.length > 0 && (
                <p className="mt-1 text-xs">Not found: {skipResult.not_found.join(", ")}</p>
              )}
            </div>
          )}

          {/* Paste domain list */}
          {failedDomains.summary.failed_count > 0 && (
            <div className="mb-4">
              <label className="block text-sm font-medium text-gray-700 mb-1">
                Paste domains to skip (one per line or comma-separated):
              </label>
              <textarea
                value={pasteList}
                onChange={(e) => setPasteList(e.target.value)}
                placeholder={"example1.com\nexample2.com\nexample3.info"}
                rows={4}
                className="w-full px-3 py-2 border rounded-lg font-mono text-sm resize-y focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
          )}

          {/* Failed domains table with checkboxes */}
          {failedDomains.failed.length > 0 && (
            <div className="mb-4">
              <div className="flex items-center justify-between mb-2">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={selectedDomains.size === failedDomains.failed.length && failedDomains.failed.length > 0}
                    onChange={(e) => {
                      if (e.target.checked) {
                        setSelectedDomains(new Set(failedDomains.failed.map((d) => d.name)));
                      } else {
                        setSelectedDomains(new Set());
                      }
                    }}
                    className="rounded"
                  />
                  <span className="font-medium text-gray-700">Select All ({failedDomains.failed.length})</span>
                </label>
                {selectedDomains.size > 0 && (
                  <span className="text-xs text-blue-600">{selectedDomains.size} selected</span>
                )}
              </div>

              <div className="max-h-64 overflow-y-auto border rounded-lg">
                <table className="min-w-full divide-y divide-gray-200">
                  <thead className="bg-gray-50 sticky top-0">
                    <tr>
                      <th className="px-3 py-2 w-8"></th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Domain</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Added</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Verified</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">DKIM</th>
                      <th className="px-3 py-2 text-left text-xs font-medium text-gray-500 uppercase">Error</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200">
                    {failedDomains.failed.map((d) => (
                      <tr key={d.id} className="hover:bg-gray-50">
                        <td className="px-3 py-2">
                          <input
                            type="checkbox"
                            checked={selectedDomains.has(d.name)}
                            onChange={(e) => {
                              const next = new Set(selectedDomains);
                              if (e.target.checked) next.add(d.name);
                              else next.delete(d.name);
                              setSelectedDomains(next);
                            }}
                            className="rounded"
                          />
                        </td>
                        <td className="px-3 py-2 text-sm font-mono">{d.name}</td>
                        <td className="px-3 py-2 text-sm">{d.domain_added ? "✅" : "❌"}</td>
                        <td className="px-3 py-2 text-sm">{d.domain_verified ? "✅" : "❌"}</td>
                        <td className="px-3 py-2 text-sm">{d.dkim_enabled ? "✅" : "❌"}</td>
                        <td className="px-3 py-2 text-xs text-red-600 max-w-xs truncate" title={d.error || ""}>
                          {d.error || "-"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Skipped domains list (collapsed) */}
          {failedDomains.skipped.length > 0 && (
            <div className="mb-4">
              <details className="text-sm">
                <summary className="cursor-pointer text-gray-500 hover:text-gray-700 font-medium">
                  ⏭️ {failedDomains.skipped.length} already skipped domains
                </summary>
                <div className="mt-2 max-h-32 overflow-y-auto border rounded-lg p-2 bg-gray-50">
                  {failedDomains.skipped.map((d) => (
                    <div key={d.id} className="text-xs text-gray-600 py-0.5 font-mono">
                      {d.name} {d.error && <span className="text-gray-400">— {d.error}</span>}
                    </div>
                  ))}
                </div>
              </details>
            </div>
          )}

          {/* Skip reason */}
          {failedDomains.summary.failed_count > 0 && (
            <div className="mb-4">
              <label className="block text-xs font-medium text-gray-700 mb-1">Reason for skipping:</label>
              <input
                type="text"
                value={skipReason}
                onChange={(e) => setSkipReason(e.target.value)}
                className="w-full px-3 py-2 border rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
              />
            </div>
          )}

          {/* Action buttons */}
          <div className="flex gap-3 flex-wrap">
            {(selectedDomains.size > 0 || pasteList.trim()) && (
              <button
                onClick={() => skipDomains()}
                disabled={isSkipping}
                className="px-4 py-2 bg-orange-600 text-white rounded-lg text-sm font-medium hover:bg-orange-700 disabled:opacity-50"
              >
                {isSkipping ? "Skipping..." : `Skip ${skipButtonCount} Domain(s)`}
              </button>
            )}

            {failedDomains.summary.failed_count > 0 && (
              <button
                onClick={() => {
                  if (confirm(`Are you sure you want to skip ALL ${failedDomains.summary.failed_count} failed domains?`)) {
                    skipDomains(undefined, true);
                  }
                }}
                disabled={isSkipping}
                className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm font-medium hover:bg-red-700 disabled:opacity-50"
              >
                {isSkipping ? "Skipping..." : `Skip All ${failedDomains.summary.failed_count} Failed`}
              </button>
            )}

            {failedDomains.summary.failed_count === 0 && failedDomains.summary.skipped_count > 0 && (
              <button
                onClick={() => {
                  resumePipeline();
                  setShowDomainPanel(false);
                }}
                className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
              >
                Resume Pipeline →
              </button>
            )}

            <button
              onClick={() => fetchFailedDomains(failedDomains.step)}
              disabled={isSkipping}
              className="px-4 py-2 border border-gray-300 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-50 disabled:opacity-50"
            >
              🔄 Refresh
            </button>
          </div>
        </div>
      )}

      {/* Errors Panel */}
      {failedSteps.length > 0 && (
        <div className="mb-8">
          <h2 className="text-lg font-semibold text-gray-900 mb-3">Errors</h2>
          <div className="space-y-2">
            {failedSteps.map((err, i) => (
              <div key={i} className="rounded-lg bg-red-50 border border-red-200 p-3 text-sm text-red-800">
                <span className="font-medium">Step {err.step}:</span> {err.error}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Activity Log */}
      <div>
        <h2 className="text-lg font-semibold text-gray-900 mb-3">Recent Activity</h2>
        <div className="border border-gray-200 rounded-lg divide-y divide-gray-100 max-h-96 overflow-y-auto">
          {activityLog.length === 0 ? (
            <p className="p-4 text-sm text-gray-400">No activity yet...</p>
          ) : (
            activityLog.map((entry, i) => (
              <div key={i} className="flex items-center justify-between px-4 py-2.5 text-sm">
                <div className="flex items-center gap-2">
                  <span className={`inline-block w-2 h-2 rounded-full ${
                    entry.status === "completed" ? "bg-green-500" :
                    entry.status === "failed" ? "bg-red-500" :
                    entry.status === "started" ? "bg-blue-500" :
                    entry.status === "skipped" ? "bg-yellow-500" :
                    "bg-gray-400"
                  }`} />
                  <span className="text-gray-600">{entry.step_name}</span>
                  {entry.item_name && (
                    <span className="font-medium text-gray-900">{entry.item_name}</span>
                  )}
                  {entry.message && (
                    <span className="text-gray-400">— {entry.message}</span>
                  )}
                </div>
                <span className="text-xs text-gray-400 whitespace-nowrap">
                  {new Date(entry.timestamp).toLocaleTimeString()}
                </span>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
