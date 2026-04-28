"use client";

import React, { useState, useEffect, useCallback } from "react";

interface TenantStep7Status {
  id: string;
  domain: string;
  step7_complete: boolean;
  smtp_auth_enabled: boolean;
  app_consent_granted: boolean;
  app_consent_error: string | null;
  app_consent_granted_at: string | null;
  security_defaults_disabled: boolean;
  security_defaults_error: string | null;
  security_defaults_disabled_at: string | null;
  conditional_access_disabled?: boolean;
  conditional_access_error?: string | null;
  conditional_access_disabled_at?: string | null;
  conditional_access_policies_disabled_count?: number;
  error: string | null;
  completed_at: string | null;
}

interface Step7Status {
  batch_complete: boolean;
  eligible: number;
  complete: number;
  failed: number;
  pending: number;
  sequencer_app_key?: string;
  sequencer_app_name?: string;
  tenants: TenantStep7Status[];
}

interface Props {
  batchId: string;
  onComplete?: () => void;
  suppressAutoComplete?: boolean;
}

export default function Step7SequencerPrep({ batchId, onComplete, suppressAutoComplete }: Props) {
  const [status, setStatus] = useState<Step7Status | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sequencerKey, setSequencerKey] = useState("instantly");
  const [sequencerTouched, setSequencerTouched] = useState(false);
  const [sequencerSaving, setSequencerSaving] = useState(false);
  const [forceCompletingTenant, setForceCompletingTenant] = useState<string | null>(null);
  const [forceCompletingAll, setForceCompletingAll] = useState(false);

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const sequencerOptions = [
    { key: "instantly", label: "Instantly.ai" },
    { key: "plusvibe", label: "PlusVibe" },
    { key: "smartlead", label: "Smartlead.ai" },
  ];

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step7/status`
      );
      if (res.ok) {
        const data: Step7Status = await res.json();
        setStatus(data);
        if (!sequencerTouched && data.sequencer_app_key) {
          setSequencerKey(data.sequencer_app_key);
        }

        // Auto-stop polling when all done
        if (data.eligible > 0 && data.complete === data.eligible) {
          setIsRunning(false);
        }

        // Notify parent if batch is fully complete
        if (data.batch_complete && !suppressAutoComplete) {
          onComplete?.();
        }
      }
    } catch (err) {
      console.error("Failed to fetch Step 7 status:", err);
    } finally {
      setLoading(false);
    }
  }, [batchId, API_BASE, onComplete, suppressAutoComplete, sequencerTouched]);

  useEffect(() => {
    fetchStatus();
  }, [fetchStatus]);

  // Poll every 5 seconds while running
  useEffect(() => {
    if (!isRunning) return;
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, [isRunning, fetchStatus]);

  const startStep7 = async () => {
    setError(null);
    setIsRunning(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step7/start`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!data.success) {
        setError(data.error || "Failed to start Step 7");
        setIsRunning(false);
      }
    } catch (err) {
      setError("Network error starting Step 7");
      setIsRunning(false);
    }
  };

  const retryFailed = async () => {
    setError(null);
    setIsRunning(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step7/retry-failed`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!data.success) {
        setError(data.error || "Failed to retry");
        setIsRunning(false);
      }
    } catch (err) {
      setError("Network error retrying");
      setIsRunning(false);
    }
  };

  const rerunAll = async () => {
    setError(null);
    setIsRunning(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step7/rerun-all`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!data.success) {
        setError(data.error || "Failed to rerun Step 7");
        setIsRunning(false);
      }
    } catch (err) {
      setError("Network error rerunning Step 7");
      setIsRunning(false);
    }
  };

  const updateSequencer = async (nextKey: string) => {
    if (!nextKey) return;
    setError(null);
    setSequencerTouched(true);
    setSequencerKey(nextKey);
    setSequencerSaving(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/sequencer`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ sequencer_app_key: nextKey }),
        }
      );
      const data = await res.json();
      if (!res.ok || !data.success) {
        throw new Error(data.detail || data.message || "Failed to update sequencer");
      }
      await fetchStatus();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update sequencer");
    } finally {
      setSequencerSaving(false);
    }
  };

  // Force complete a single tenant
  const forceCompleteTenant = async (tenantId: string) => {
    if (!confirm(`Force-complete Step 7 for this tenant? Use only if Security Defaults are already disabled, SMTP Auth is enabled, and ${appName} consent is granted. This updates the database and skips automation.`)) return;
    
    setForceCompletingTenant(tenantId);
    setError(null);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step7/force-complete/${tenantId}`,
        { method: "POST" }
      );
      const data = await res.json();
      if (data.success) {
        await fetchStatus();
      } else {
        setError(data.message || "Failed to force complete tenant");
      }
    } catch (err) {
      setError("Network error force completing tenant");
    } finally {
      setForceCompletingTenant(null);
    }
  };

  // Force complete ALL pending tenants
  const forceCompleteAll = async () => {
    if (!confirm(`Force-complete Step 7 for ALL pending tenants? Use only if Security Defaults are already disabled, SMTP Auth is enabled, and ${appName} consent is granted for each tenant. This updates the database and completes the batch.`)) return;
    
    setForceCompletingAll(true);
    setError(null);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step7/force-complete-all`,
        { method: "POST" }
      );
      const data = await res.json();
      if (data.success) {
        await fetchStatus();
        alert(`Force-completed ${data.updated_count} tenant(s). Batch marked as complete.`);
      } else {
        setError(data.message || "Failed to force complete all tenants");
      }
    } catch (err) {
      setError("Network error force completing all tenants");
    } finally {
      setForceCompletingAll(false);
    }
  };

  if (loading) {
    return <div className="p-6 text-gray-500">Loading Step 7 status...</div>;
  }

  const selectedSequencer = sequencerOptions.find((opt) => opt.key === sequencerKey);
  const appName = status?.sequencer_app_name || selectedSequencer?.label || "Sequencer";

  const allComplete =
    status && status.eligible > 0 && status.complete === status.eligible;

  const getStatusBadge = (t: TenantStep7Status) => {
    if (t.step7_complete) {
      return {
        label: "Done",
        className:
          "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800",
      };
    }

    const errorText = t.error || t.security_defaults_error || t.app_consent_error;
    if (errorText) {
      const lower = errorText.toLowerCase();
      let stage = "";
      if (lower.includes("security defaults")) stage = "Security Defaults";
      if (lower.includes("smtp auth")) stage = "SMTP Auth";
      if (lower.includes("consent")) stage = "App Consent";
      const label = stage ? `Failed (${stage})` : "Failed";
      return {
        label,
        className:
          "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800",
      };
    }

    if (t.security_defaults_disabled && t.smtp_auth_enabled && !t.app_consent_granted) {
      return {
        label: "Pending (Consent)",
        className:
          "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800",
      };
    }

    if (t.security_defaults_disabled && !t.smtp_auth_enabled) {
      return {
        label: "Partial (SMTP)",
        className:
          "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800",
      };
    }

    return {
      label: "Pending",
      className:
        "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600",
    };
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-gray-900">
          Step 7: SMTP Auth + {appName} Consent
        </h2>
        <p className="mt-1 text-sm text-gray-500">
          Enables SMTP Authentication at the organization level and grants{" "}
          {appName} admin consent so mailboxes can be connected to
          <strong> {appName}</strong> or other email sequencers.
        </p>
      </div>

      {/* Sequencer selection */}
      <div className="rounded-lg border bg-white p-4">
        <label className="block text-sm font-medium text-gray-700">
          Sequencer for Step 7 Consent
        </label>
        <div className="mt-2 flex flex-wrap gap-3 items-center">
          <select
            value={sequencerKey}
            onChange={(e) => updateSequencer(e.target.value)}
            disabled={isRunning || sequencerSaving}
            className="min-w-[220px] px-3 py-2 border rounded-lg bg-white"
          >
            {sequencerOptions.map((opt) => (
              <option key={opt.key} value={opt.key}>
                {opt.label}
              </option>
            ))}
          </select>
          {sequencerSaving && (
            <span className="text-xs text-gray-500">Saving...</span>
          )}
        </div>
        <p className="mt-2 text-xs text-gray-500">
          Changing the sequencer resets consent status for this batch.
        </p>
        <p className="mt-1 text-xs text-gray-500">
          Consent is granted via the popup only; scope patching is disabled.
        </p>
      </div>

      {/* Info banner */}
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-700">
        <p className="font-medium">What this does:</p>
        <p className="mt-1">
          1) Enables SMTP Auth via Exchange Online PowerShell.
        </p>
        <p className="mt-1">
          2) Grants {appName} admin consent so OAuth connections succeed.
        </p>
        <p className="mt-2 text-blue-600">
          This can take a few minutes per tenant on Railway or low-resource
          hosts.
        </p>
        <p className="mt-2 text-blue-600">
          <strong>Note:</strong> Microsoft may take up to 1 hour to fully sync
          this change. Sequencer connections may not work immediately.
        </p>
      </div>

      {/* Status grid */}
      {status && (
        <div className="grid grid-cols-4 gap-4">
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-gray-900">
              {status.eligible}
            </p>
            <p className="text-sm text-gray-500">Eligible</p>
          </div>
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-green-600">
              {status.complete}
            </p>
            <p className="text-sm text-gray-500">Complete</p>
          </div>
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-yellow-600">
              {status.pending}
            </p>
            <p className="text-sm text-gray-500">Pending</p>
          </div>
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-red-600">
              {status.failed}
            </p>
            <p className="text-sm text-gray-500">Failed</p>
          </div>
        </div>
      )}

      {/* Progress bar */}
      {status && status.eligible > 0 && (
        <div className="w-full bg-gray-200 rounded-full h-3">
          <div
            className={`h-3 rounded-full transition-all duration-500 ${
              allComplete ? "bg-green-500" : "bg-blue-500"
            }`}
            style={{
              width: `${Math.round(
                (status.complete / status.eligible) * 100
              )}%`,
            }}
          />
        </div>
      )}

      {/* Action buttons */}
      <div className="flex flex-wrap gap-3">
        {!allComplete && (
          <button
            onClick={startStep7}
            disabled={isRunning || !status || status.eligible === 0}
            className={`px-6 py-2.5 rounded-lg font-medium text-white transition-colors ${
              isRunning || !status || status.eligible === 0
                ? "bg-gray-400 cursor-not-allowed"
                : "bg-blue-600 hover:bg-blue-700"
            }`}
          >
            {isRunning ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle
                    className="opacity-25"
                    cx="12"
                    cy="12"
                    r="10"
                    stroke="currentColor"
                    strokeWidth="4"
                    fill="none"
                  />
                  <path
                    className="opacity-75"
                    fill="currentColor"
                    d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
                  />
                </svg>
                Running Step 7...
              </span>
            ) : status?.complete && status.complete > 0 ? (
              "Continue Processing"
            ) : (
              "Run Step 7"
            )}
          </button>
        )}

        {status && status.failed > 0 && (
          <button
            onClick={retryFailed}
            disabled={isRunning}
            className={`px-6 py-2.5 rounded-lg font-medium transition-colors ${
              isRunning
                ? "bg-gray-200 text-gray-400 cursor-not-allowed"
                : "bg-orange-100 text-orange-700 hover:bg-orange-200"
            }`}
          >
            Retry {status.failed} Failed
          </button>
        )}

        {status && status.eligible > 0 && (
          <button
            onClick={rerunAll}
            disabled={isRunning}
            className={`px-6 py-2.5 rounded-lg font-medium transition-colors ${
              isRunning
                ? "bg-gray-200 text-gray-400 cursor-not-allowed"
                : "bg-gray-100 text-gray-700 hover:bg-gray-200"
            }`}
          >
            Rerun Step 7
          </button>
        )}

        {/* Force Complete All - shown when there are pending/failed tenants */}
        {status && status.pending + status.failed > 0 && !isRunning && (
          <button
            onClick={forceCompleteAll}
            disabled={forceCompletingAll}
            className={`px-6 py-2.5 rounded-lg font-medium transition-colors ${
              forceCompletingAll
                ? "bg-gray-200 text-gray-400 cursor-not-allowed"
                : "bg-yellow-500 text-white hover:bg-yellow-600"
            }`}
          >
            {forceCompletingAll ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                Force Completing...
              </span>
            ) : (
              `⚡ Force Complete All (${status.pending + status.failed})`
            )}
          </button>
        )}
      </div>

      {/* Force Complete Info */}
      {status && status.pending + status.failed > 0 && !isRunning && (
        <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-800">
          <p><strong>💡 Manual Override:</strong> If you've already disabled Security Defaults, enabled SMTP Auth, and granted {appName} consent manually, use "Force Complete" to update the database without re-running automation.</p>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Success — Batch Complete */}
      {allComplete && (
        <div className="rounded-lg border border-green-200 bg-green-50 p-4">
          <p className="text-lg font-semibold text-green-700">
            Setup Complete!
          </p>
          <p className="mt-1 text-sm text-green-600">
            All tenants have Security Defaults disabled, SMTP Auth enabled,
            and {appName} consent granted. Your mailboxes are ready to upload to
            {appName} or any other sequencer.
          </p>
          <div className="mt-3 rounded border border-green-300 bg-white p-3 text-xs font-mono text-gray-700 space-y-1">
            <p><strong>SMTP Host:</strong> smtp.office365.com &nbsp;|&nbsp; <strong>Port:</strong> 587</p>
            <p><strong>IMAP Host:</strong> outlook.office365.com &nbsp;|&nbsp; <strong>Port:</strong> 993</p>
            <p><strong>Encryption:</strong> TLS / STARTTLS</p>
            <p><strong>Username:</strong> mailbox email address</p>
            <p><strong>Password:</strong> password set in Step 6</p>
          </div>
          <p className="mt-2 text-xs text-green-500">
            Allow up to 1 hour for Microsoft to fully propagate SMTP Auth
            changes before testing connections.
          </p>
        </div>
      )}

      {/* Tenant table */}
      {status && status.tenants.length > 0 && (
        <div className="rounded-lg border overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Domain
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">
                  SMTP Auth
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">
                  {appName} Consent
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">
                  Security Defaults
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">
                  Status
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Error
                </th>
                <th className="px-4 py-3 text-right text-xs font-medium text-gray-500 uppercase">
                  Actions
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {status.tenants.map((t) => (
                <tr key={t.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-sm font-medium text-gray-900">
                    {t.domain}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {t.smtp_auth_enabled ? (
                      <span className="text-green-500 text-lg">&#10003;</span>
                    ) : (
                      <span className="text-gray-300">&mdash;</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {t.app_consent_granted ? (
                      <span className="text-green-500 text-lg">&#10003;</span>
                    ) : (
                      <span className="text-gray-300">&mdash;</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {t.security_defaults_disabled ? (
                      <span className="inline-flex items-center gap-1">
                        <span className="text-green-500 text-lg">&#10003;</span>
                        {t.conditional_access_disabled && (
                          <span
                            title={`Disabled ${t.conditional_access_policies_disabled_count ?? 0} Microsoft-managed Conditional Access MFA policy(ies)`}
                            className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold bg-purple-100 text-purple-800"
                          >
                            via CA
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="text-gray-300">&mdash;</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {(() => {
                      const badge = getStatusBadge(t);
                      return (
                        <span className={badge.className}>
                          {badge.label}
                        </span>
                      );
                    })()}
                  </td>
                  <td className="px-4 py-3 text-sm text-red-500 max-w-xs truncate">
                    {t.error || t.security_defaults_error || t.conditional_access_error || t.app_consent_error || "\u2014"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {!t.step7_complete && (
                      <button
                        onClick={() => forceCompleteTenant(t.id)}
                        disabled={forceCompletingTenant === t.id || isRunning}
                        className={`px-2 py-1 text-xs rounded transition-colors ${
                          forceCompletingTenant === t.id || isRunning
                            ? "bg-gray-100 text-gray-400 cursor-not-allowed"
                            : "bg-yellow-100 text-yellow-800 hover:bg-yellow-200"
                        }`}
                      >
                        {forceCompletingTenant === t.id ? "..." : "Force Complete"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
