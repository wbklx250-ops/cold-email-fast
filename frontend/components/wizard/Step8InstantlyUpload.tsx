"use client";

import React, { useState, useEffect, useCallback } from "react";

interface MailboxUploadStatus {
  mailbox_id: string;
  mailbox_email: string;
  tenant_domain: string;
  status: "pending" | "uploading" | "uploaded" | "failed";
  error: string | null;
  uploaded_at: string | null;
}

interface Step8Status {
  batch_complete: boolean;
  total: number;
  uploaded: number;
  failed: number;
  pending: number;
  uploading: number;
  mailboxes: MailboxUploadStatus[];
}

interface InstantlyAccount {
  id: string;
  label: string;
  email: string;
  is_default: boolean;
  created_at: string;
}

interface Props {
  batchId: string;
  onComplete?: () => void;
  suppressAutoComplete?: boolean;
}

export default function Step8InstantlyUpload({ batchId, onComplete, suppressAutoComplete }: Props) {
  const [status, setStatus] = useState<Step8Status | null>(null);
  const [accounts, setAccounts] = useState<InstantlyAccount[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Configuration state
  const [useExistingAccount, setUseExistingAccount] = useState(true);
  const [selectedAccountId, setSelectedAccountId] = useState<string>("");
  const [instantlyEmail, setInstantlyEmail] = useState("");
  const [instantlyPassword, setInstantlyPassword] = useState("");
  const [numWorkers, setNumWorkers] = useState(3);
  const [headless, setHeadless] = useState(true);
  const [skipUploaded, setSkipUploaded] = useState(true);

  // Account management state
  const [showAddAccount, setShowAddAccount] = useState(false);
  const [newAccountLabel, setNewAccountLabel] = useState("");
  const [newAccountEmail, setNewAccountEmail] = useState("");
  const [newAccountPassword, setNewAccountPassword] = useState("");
  const [savingAccount, setSavingAccount] = useState(false);

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/status`
      );
      if (res.ok) {
        const data = await res.json();
        const convertedStatus: Step8Status = {
          batch_complete: (data.summary?.total || 0) > 0 && (data.summary?.uploaded || 0) === (data.summary?.total || 0),
          total: data.summary?.total || 0,
          uploaded: data.summary?.uploaded || 0,
          failed: data.summary?.failed || 0,
          pending: data.summary?.pending || 0,
          uploading: 0,
          mailboxes: [],
        };
        setStatus(convertedStatus);

        const jobStatus = data.job?.status;
        if (jobStatus === "running") {
          setIsRunning(true);
        } else if (jobStatus === "completed" || jobStatus === "failed" || convertedStatus.batch_complete) {
          setIsRunning(false);
        }

        // Notify parent if batch is fully complete
        if (convertedStatus.batch_complete && !suppressAutoComplete) {
          onComplete?.();
        }
      }
    } catch (err) {
      console.error("Failed to fetch Step 8 status:", err);
    } finally {
      setLoading(false);
    }
  }, [batchId, API_BASE, onComplete, suppressAutoComplete]);

  const fetchAccounts = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/wizard/instantly/accounts`);
      if (res.ok) {
        const data = await res.json();
        setAccounts(data.accounts || []);
        
        // Auto-select default account if available
        const defaultAccount = data.accounts?.find((a: InstantlyAccount) => a.is_default);
        if (defaultAccount && !selectedAccountId) {
          setSelectedAccountId(defaultAccount.id);
        }
      }
    } catch (err) {
      console.error("Failed to fetch Instantly accounts:", err);
    }
  }, [API_BASE, selectedAccountId]);

  useEffect(() => {
    fetchStatus();
    fetchAccounts();
  }, [fetchStatus, fetchAccounts]);

  // Poll every 5 seconds while running
  useEffect(() => {
    if (!isRunning) return;
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, [isRunning, fetchStatus]);

  const startUpload = async () => {
    setError(null);
    
    // Validate inputs
    let email = "";
    let password = "";
    
    if (useExistingAccount) {
      if (!selectedAccountId) {
        setError("Please select an Instantly account");
        return;
      }
      const account = accounts.find(a => a.id === selectedAccountId);
      if (!account) {
        setError("Selected account not found");
        return;
      }
      email = account.email;
      // Password will be retrieved from backend for saved accounts
      password = ""; // Backend will use saved password
    } else {
      if (!instantlyEmail || !instantlyPassword) {
        setError("Please enter Instantly email and password");
        return;
      }
      email = instantlyEmail;
      password = instantlyPassword;
    }

    setIsRunning(true);
    try {
      const payload: any = {
        num_workers: numWorkers,
        headless: headless,
        skip_uploaded: skipUploaded,
      };

      if (useExistingAccount && selectedAccountId) {
        payload.account_id = selectedAccountId;
      } else {
        payload.instantly_email = email;
        payload.instantly_password = password;
      }

      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/start`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        }
      );
      const data = await res.json();
      if (!data.success) {
        setError(data.error || data.message || "Failed to start upload");
        setIsRunning(false);
      }
    } catch (err) {
      setError("Network error starting upload");
      setIsRunning(false);
    }
  };

  const retryFailed = async () => {
    setError(null);
    setIsRunning(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/retry-failed`,
        { method: "POST" }
      );
      const data = await res.json();
      if (!data.success) {
        setError(data.error || data.message || "Failed to retry");
        setIsRunning(false);
      }
    } catch (err) {
      setError("Network error retrying failed uploads");
      setIsRunning(false);
    }
  };

  const saveNewAccount = async () => {
    if (!newAccountLabel || !newAccountEmail || !newAccountPassword) {
      setError("Please fill in all account fields");
      return;
    }

    setSavingAccount(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/wizard/instantly/accounts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          label: newAccountLabel,
          email: newAccountEmail,
          password: newAccountPassword,
          is_default: accounts.length === 0, // First account is default
        }),
      });
      const data = await res.json();
      if (data.success && data.account) {
        await fetchAccounts();
        setSelectedAccountId(data.account.id);
        setUseExistingAccount(true);
        setShowAddAccount(false);
        setNewAccountLabel("");
        setNewAccountEmail("");
        setNewAccountPassword("");
      } else {
        setError(data.message || "Failed to save account");
      }
    } catch (err) {
      setError("Network error saving account");
    } finally {
      setSavingAccount(false);
    }
  };

  const deleteAccount = async (accountId: string) => {
    if (!confirm("Delete this Instantly account?")) return;

    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/instantly/accounts/${accountId}`,
        { method: "DELETE" }
      );
      if (res.ok) {
        await fetchAccounts();
        if (selectedAccountId === accountId) {
          setSelectedAccountId("");
        }
      }
    } catch (err) {
      setError("Failed to delete account");
    }
  };

  if (loading) {
    return <div className="p-6 text-gray-500">Loading Step 8 status...</div>;
  }

  const allComplete = status && status.total > 0 && status.uploaded === status.total;

  const getStatusBadge = (m: MailboxUploadStatus) => {
    if (m.status === "uploaded") {
      return {
        label: "Uploaded",
        className: "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800",
      };
    }
    if (m.status === "uploading") {
      return {
        label: "Uploading...",
        className: "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800",
      };
    }
    if (m.status === "failed") {
      return {
        label: "Failed",
        className: "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800",
      };
    }
    return {
      label: "Pending",
      className: "inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600",
    };
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-gray-900">
          Step 8: Upload Mailboxes to Instantly.ai
        </h2>
        <p className="mt-1 text-sm text-gray-500">
          Automatically upload all configured mailboxes to your Instantly.ai account using Selenium automation.
        </p>
      </div>

      {/* Account Selection */}
      <div className="rounded-lg border bg-white p-4 space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-2">
            Instantly.ai Account
          </label>
          <div className="flex gap-4 mb-3">
            <label className="flex items-center">
              <input
                type="radio"
                checked={useExistingAccount}
                onChange={() => setUseExistingAccount(true)}
                className="mr-2"
              />
              Use Saved Account
            </label>
            <label className="flex items-center">
              <input
                type="radio"
                checked={!useExistingAccount}
                onChange={() => setUseExistingAccount(false)}
                className="mr-2"
              />
              Enter Credentials
            </label>
          </div>

          {useExistingAccount ? (
            <div className="space-y-3">
              <select
                value={selectedAccountId}
                onChange={(e) => setSelectedAccountId(e.target.value)}
                disabled={isRunning || accounts.length === 0}
                className="w-full px-3 py-2 border rounded-lg bg-white"
              >
                <option value="">-- Select Account --</option>
                {accounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.label} ({acc.email}) {acc.is_default ? " - Default" : ""}
                  </option>
                ))}
              </select>

              {accounts.length === 0 && (
                <p className="text-sm text-gray-500">No saved accounts. Add one below.</p>
              )}

              <button
                onClick={() => setShowAddAccount(!showAddAccount)}
                className="text-sm text-blue-600 hover:text-blue-700"
              >
                {showAddAccount ? "Cancel" : "+ Add New Account"}
              </button>

              {showAddAccount && (
                <div className="border rounded-lg p-4 bg-gray-50 space-y-3">
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">Label</label>
                    <input
                      type="text"
                      value={newAccountLabel}
                      onChange={(e) => setNewAccountLabel(e.target.value)}
                      placeholder="e.g., Main Account"
                      className="w-full px-3 py-2 border rounded-lg"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">Email</label>
                    <input
                      type="email"
                      value={newAccountEmail}
                      onChange={(e) => setNewAccountEmail(e.target.value)}
                      placeholder="your@instantly.ai"
                      className="w-full px-3 py-2 border rounded-lg"
                    />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">Password</label>
                    <input
                      type="password"
                      value={newAccountPassword}
                      onChange={(e) => setNewAccountPassword(e.target.value)}
                      placeholder="Password"
                      className="w-full px-3 py-2 border rounded-lg"
                    />
                  </div>
                  <button
                    onClick={saveNewAccount}
                    disabled={savingAccount}
                    className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-400"
                  >
                    {savingAccount ? "Saving..." : "Save Account"}
                  </button>
                </div>
              )}

              {/* Saved accounts list with delete option */}
              {accounts.length > 0 && !showAddAccount && (
                <div className="mt-3 space-y-2">
                  <p className="text-xs font-medium text-gray-700">Saved Accounts:</p>
                  {accounts.map((acc) => (
                    <div key={acc.id} className="flex items-center justify-between text-sm p-2 bg-gray-50 rounded">
                      <span>{acc.label} - {acc.email}</span>
                      <button
                        onClick={() => deleteAccount(acc.id)}
                        className="text-red-600 hover:text-red-700 text-xs"
                      >
                        Delete
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-700 mb-1">Instantly Email</label>
                <input
                  type="email"
                  value={instantlyEmail}
                  onChange={(e) => setInstantlyEmail(e.target.value)}
                  placeholder="your@instantly.ai"
                  className="w-full px-3 py-2 border rounded-lg"
                  disabled={isRunning}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-700 mb-1">Instantly Password</label>
                <input
                  type="password"
                  value={instantlyPassword}
                  onChange={(e) => setInstantlyPassword(e.target.value)}
                  placeholder="Password"
                  className="w-full px-3 py-2 border rounded-lg"
                  disabled={isRunning}
                />
              </div>
            </div>
          )}
        </div>

        {/* Configuration Options */}
        <div className="pt-4 border-t space-y-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Parallel Workers: {numWorkers}
            </label>
            <input
              type="range"
              min="1"
              max="5"
              value={numWorkers}
              onChange={(e) => setNumWorkers(parseInt(e.target.value))}
              disabled={isRunning}
              className="w-full"
            />
            <p className="text-xs text-gray-500 mt-1">
              Higher values = faster, but may cause rate limiting
            </p>
          </div>

          <label className="flex items-center">
            <input
              type="checkbox"
              checked={headless}
              onChange={(e) => setHeadless(e.target.checked)}
              disabled={isRunning}
              className="mr-2"
            />
            <span className="text-sm text-gray-700">Run in headless mode (no browser UI)</span>
          </label>

          <label className="flex items-center">
            <input
              type="checkbox"
              checked={skipUploaded}
              onChange={(e) => setSkipUploaded(e.target.checked)}
              disabled={isRunning}
              className="mr-2"
            />
            <span className="text-sm text-gray-700">Skip already uploaded mailboxes</span>
          </label>
        </div>
      </div>

      {/* Info banner */}
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-700">
        <p className="font-medium">What this does:</p>
        <p className="mt-1">
          1) Logs into your Instantly.ai account using Selenium WebDriver
        </p>
        <p className="mt-1">
          2) Navigates to the email accounts page and uploads each mailbox
        </p>
        <p className="mt-1">
          3) Uses parallel workers to process multiple mailboxes simultaneously
        </p>
        <p className="mt-2 text-blue-600">
          <strong>Note:</strong> This process can take several minutes depending on the number of mailboxes and workers.
        </p>
      </div>

      {/* Status grid */}
      {status && (
        <div className="grid grid-cols-4 gap-4">
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-gray-900">{status.total}</p>
            <p className="text-sm text-gray-500">Total</p>
          </div>
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-green-600">{status.uploaded}</p>
            <p className="text-sm text-gray-500">Uploaded</p>
          </div>
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-yellow-600">{status.pending}</p>
            <p className="text-sm text-gray-500">Pending</p>
          </div>
          <div className="rounded-lg border p-4 text-center">
            <p className="text-2xl font-bold text-red-600">{status.failed}</p>
            <p className="text-sm text-gray-500">Failed</p>
          </div>
        </div>
      )}

      {/* Progress bar */}
      {status && status.total > 0 && (
        <div className="w-full bg-gray-200 rounded-full h-3">
          <div
            className={`h-3 rounded-full transition-all duration-500 ${
              allComplete ? "bg-green-500" : "bg-blue-500"
            }`}
            style={{
              width: `${Math.round((status.uploaded / status.total) * 100)}%`,
            }}
          />
        </div>
      )}

      {/* Action buttons */}
      <div className="flex flex-wrap gap-3">
        {!allComplete && (
          <button
            onClick={startUpload}
            disabled={isRunning || !status || status.total === 0}
            className={`px-6 py-2.5 rounded-lg font-medium text-white transition-colors ${
              isRunning || !status || status.total === 0
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
                Uploading...
              </span>
            ) : status?.uploaded && status.uploaded > 0 ? (
              "Continue Upload"
            ) : (
              "Start Upload"
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
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      {/* Success */}
      {allComplete && (
        <div className="rounded-lg border border-green-200 bg-green-50 p-4">
          <p className="text-lg font-semibold text-green-700">
            Upload Complete! 🎉
          </p>
          <p className="mt-1 text-sm text-green-600">
            All {status.total} mailboxes have been successfully uploaded to Instantly.ai.
            You can now manage them from your Instantly dashboard.
          </p>
        </div>
      )}

      {/* Mailbox table */}
      {status && status.mailboxes.length > 0 && (
        <div className="rounded-lg border overflow-hidden">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Mailbox
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Domain
                </th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">
                  Status
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Error
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">
                  Uploaded At
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {status.mailboxes.map((m) => (
                <tr key={m.mailbox_id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 text-sm font-medium text-gray-900">
                    {m.mailbox_email}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600">
                    {m.tenant_domain}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {(() => {
                      const badge = getStatusBadge(m);
                      return <span className={badge.className}>{badge.label}</span>;
                    })()}
                  </td>
                  <td className="px-4 py-3 text-sm text-red-500 max-w-xs truncate">
                    {m.error || "—"}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500">
                    {m.uploaded_at
                      ? new Date(m.uploaded_at).toLocaleString()
                      : "—"}
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
