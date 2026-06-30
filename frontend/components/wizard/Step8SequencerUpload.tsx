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
  has_api_key: boolean;
  is_default: boolean;
  created_at: string;
}

interface Props {
  batchId: string;
  onComplete?: () => void;
  suppressAutoComplete?: boolean;
}

export default function Step8SequencerUpload({ batchId, onComplete, suppressAutoComplete }: Props) {
  const [sequencer, setSequencer] = useState<"instantly" | "smartlead">("instantly");
  const [status, setStatus] = useState<Step8Status | null>(null);
  const [accounts, setAccounts] = useState<InstantlyAccount[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  
  // Instantly configuration state
  const [useExistingAccount, setUseExistingAccount] = useState(true);
  const [selectedAccountId, setSelectedAccountId] = useState<string>("");
  const [instantlyEmail, setInstantlyEmail] = useState("");
  const [instantlyPassword, setInstantlyPassword] = useState("");
  
  // Smartlead configuration state
  const [smartleadApiKey, setSmartleadApiKey] = useState("");
  const [smartleadOAuthUrl, setSmartleadOAuthUrl] = useState("");
  const [configureSettings, setConfigureSettings] = useState(true);
  const [maxEmailPerDay, setMaxEmailPerDay] = useState(6);
  const [waitMins, setWaitMins] = useState(60);
  const [warmupPerDay, setWarmupPerDay] = useState(40);
  const [rampup, setRampup] = useState(1);
  const [replyRate, setReplyRate] = useState(79);
  
  // Shared configuration
  const [numWorkers, setNumWorkers] = useState(2);
  const [skipUploaded, setSkipUploaded] = useState(true);

  // Account management state
  const [showAddAccount, setShowAddAccount] = useState(false);
  const [newAccountLabel, setNewAccountLabel] = useState("");
  const [newAccountEmail, setNewAccountEmail] = useState("");
  const [newAccountPassword, setNewAccountPassword] = useState("");
  const [newAccountApiKey, setNewAccountApiKey] = useState("");
  const [savingAccount, setSavingAccount] = useState(false);
  const [editingAccountId, setEditingAccountId] = useState<string | null>(null);
  const [editAccountLabel, setEditAccountLabel] = useState("");
  const [editAccountEmail, setEditAccountEmail] = useState("");
  const [editAccountPassword, setEditAccountPassword] = useState("");
  const [editAccountApiKey, setEditAccountApiKey] = useState("");

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  const fetchStatus = useCallback(async () => {
    try {
      const endpoint = sequencer === "instantly" 
        ? `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/status`
        : `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/smartlead/status`;
      
      const res = await fetch(endpoint);
      if (res.ok) {
        const data = await res.json();
        // Convert backend format to Step8Status format
        const convertedStatus: Step8Status = {
          batch_complete: (data.summary?.total || 0) > 0 && (data.summary?.uploaded || 0) === (data.summary?.total || 0),
          total: data.summary?.total || 0,
          uploaded: data.summary?.uploaded || 0,
          failed: data.summary?.failed || 0,
          pending: data.summary?.pending || 0,
          uploading: 0,
          mailboxes: []
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
  }, [batchId, API_BASE, onComplete, suppressAutoComplete, sequencer]);

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

  // Load Smartlead creds from localStorage on mount
  useEffect(() => {
    try {
      const savedApiKey = localStorage.getItem("smartlead_api_key");
      const savedOAuthUrl = localStorage.getItem("smartlead_oauth_url");
      if (savedApiKey) setSmartleadApiKey(savedApiKey);
      if (savedOAuthUrl) setSmartleadOAuthUrl(savedOAuthUrl);
    } catch (_) {}
  }, []);

  useEffect(() => {
    fetchStatus();
    if (sequencer === "instantly") {
      fetchAccounts();
    }
  }, [fetchStatus, fetchAccounts, sequencer]);

  // Poll every 5 seconds while running
  useEffect(() => {
    if (!isRunning) return;
    const interval = setInterval(fetchStatus, 5000);
    return () => clearInterval(interval);
  }, [isRunning, fetchStatus]);

  const startUpload = async () => {
    setError(null);
    
    if (sequencer === "instantly") {
      // Validate Instantly inputs
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
    } else {
      // Validate Smartlead inputs
      const trimmedSmartleadApiKey = smartleadApiKey.trim();
      const trimmedSmartleadOAuthUrl = smartleadOAuthUrl.trim();
      if (!trimmedSmartleadOAuthUrl) {
        setError("Please enter the Smartlead OAuth URL");
        return;
      }

      setIsRunning(true);
      try {
        // Save Smartlead creds to localStorage for persistence
        try {
          if (trimmedSmartleadApiKey) {
            localStorage.setItem("smartlead_api_key", trimmedSmartleadApiKey);
          } else {
            localStorage.removeItem("smartlead_api_key");
          }
          localStorage.setItem("smartlead_oauth_url", trimmedSmartleadOAuthUrl);
        } catch (_) {}

        const payload = {
          api_key: trimmedSmartleadApiKey || undefined,
          oauth_url: trimmedSmartleadOAuthUrl,
          num_workers: numWorkers,
          skip_uploaded: skipUploaded,
          configure_settings: Boolean(trimmedSmartleadApiKey) && configureSettings,
          max_email_per_day: maxEmailPerDay,
          time_to_wait_in_mins: waitMins,
          total_warmup_per_day: warmupPerDay,
          daily_rampup: rampup,
          reply_rate_percentage: replyRate,
        };

        const res = await fetch(
          `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/smartlead/start`,
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
    }
  };

  const retryFailed = async () => {
    setError(null);
    setIsRunning(true);
    try {
      const endpoint = sequencer === "instantly"
        ? `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/retry-failed`
        : `${API_BASE}/api/v1/wizard/batches/${batchId}/step8/smartlead/retry-failed`;

      const requestInit: RequestInit = { method: "POST" };
      if (sequencer === "smartlead") {
        const trimmedSmartleadApiKey = smartleadApiKey.trim();
        const trimmedSmartleadOAuthUrl = smartleadOAuthUrl.trim();
        if (!trimmedSmartleadOAuthUrl) {
          setError("Please enter the Smartlead OAuth URL");
          setIsRunning(false);
          return;
        }
        requestInit.headers = { "Content-Type": "application/json" };
        requestInit.body = JSON.stringify({
          api_key: trimmedSmartleadApiKey || undefined,
          oauth_url: trimmedSmartleadOAuthUrl,
          num_workers: numWorkers,
          skip_uploaded: true,
          configure_settings: Boolean(trimmedSmartleadApiKey) && configureSettings,
          max_email_per_day: maxEmailPerDay,
          time_to_wait_in_mins: waitMins,
          total_warmup_per_day: warmupPerDay,
          daily_rampup: rampup,
          reply_rate_percentage: replyRate,
        });
      }

      const res = await fetch(endpoint, requestInit);
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
          api_key: newAccountApiKey || undefined,
          is_default: accounts.length === 0,
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
        setNewAccountApiKey("");
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

  const startEditAccount = (account: InstantlyAccount) => {
    setShowAddAccount(false);
    setEditingAccountId(account.id);
    setEditAccountLabel(account.label);
    setEditAccountEmail(account.email);
    setEditAccountPassword("");
    setEditAccountApiKey("");
  };

  const cancelEditAccount = () => {
    setEditingAccountId(null);
    setEditAccountLabel("");
    setEditAccountEmail("");
    setEditAccountPassword("");
    setEditAccountApiKey("");
  };

  const updateAccount = async () => {
    if (!editingAccountId) return;
    if (!editAccountLabel || !editAccountEmail) {
      setError("Please fill in label and email");
      return;
    }

    setSavingAccount(true);
    setError(null);
    try {
      const res = await fetch(
        `${API_BASE}/api/v1/wizard/instantly/accounts/${editingAccountId}`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            label: editAccountLabel,
            email: editAccountEmail,
            password: editAccountPassword || undefined,
            api_key: editAccountApiKey || undefined,
          }),
        }
      );
      const data = await res.json();
      if (data.success) {
        await fetchAccounts();
        cancelEditAccount();
      } else {
        setError(data.message || "Failed to update account");
      }
    } catch (err) {
      setError("Network error updating account");
    } finally {
      setSavingAccount(false);
    }
  };

  if (loading) {
    return <div className="p-6 text-gray-500">Loading Step 8 status...</div>;
  }

  const allComplete = status && status.total > 0 && status.uploaded === status.total;
  const hasSmartleadApiKey = smartleadApiKey.trim().length > 0;
  const smartleadSettingsEnabled = hasSmartleadApiKey && configureSettings;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-gray-900">
          Step 8: Upload Mailboxes to Sequencer
        </h2>
        <p className="mt-1 text-sm text-gray-500">
          Automatically upload all configured mailboxes to your email sequencer using OAuth automation.
        </p>
      </div>

      {/* Sequencer Selection */}
      <div className="rounded-lg border bg-white p-4">
        <label className="block text-sm font-medium text-gray-700 mb-2">
          Select Sequencer
        </label>
        <div className="flex gap-4 mb-3">
          <label className="flex items-center">
            <input
              type="radio"
              checked={sequencer === "instantly"}
              onChange={() => setSequencer("instantly")}
              disabled={isRunning}
              className="mr-2"
            />
            <span className="text-sm text-gray-700">⚡ Instantly.ai</span>
          </label>
          <label className="flex items-center">
            <input
              type="radio"
              checked={sequencer === "smartlead"}
              onChange={() => setSequencer("smartlead")}
              disabled={isRunning}
              className="mr-2"
            />
            <span className="text-sm text-gray-700">🚀 Smartlead.ai</span>
          </label>
        </div>
      </div>

      {/* Instantly-specific configuration */}
      {sequencer === "instantly" && (
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
                    <div>
                      <label className="block text-xs font-medium text-gray-700 mb-1">
                        API Key <span className="text-gray-400 font-normal">(optional, for verification)</span>
                      </label>
                      <input
                        type="text"
                        value={newAccountApiKey}
                        onChange={(e) => setNewAccountApiKey(e.target.value)}
                        placeholder="sk_..."
                        className="w-full px-3 py-2 border rounded-lg font-mono text-sm"
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

                {/* Selected account API key indicator */}
                {selectedAccountId && (() => {
                  const sel = accounts.find(a => a.id === selectedAccountId);
                  return sel?.has_api_key ? (
                    <p className="text-xs text-green-600 mt-1">
                      ✓ Saved API key will be used for upload verification
                    </p>
                  ) : null;
                })()}

                {/* Saved accounts list with edit/delete options */}
                {accounts.length > 0 && !showAddAccount && (
                  <div className="mt-3 space-y-2">
                    <p className="text-xs font-medium text-gray-700">Saved Accounts:</p>
                    {accounts.map((acc) => (
                      <div key={acc.id} className="rounded bg-gray-50">
                        {editingAccountId === acc.id ? (
                          <div className="border rounded-lg p-4 bg-white space-y-3">
                            <div className="flex items-center justify-between">
                              <p className="text-sm font-medium text-gray-700">Edit Account</p>
                              {acc.has_api_key && (
                                <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
                                  🔑 API Key Saved
                                </span>
                              )}
                            </div>
                            <div>
                              <label className="block text-xs font-medium text-gray-700 mb-1">Label</label>
                              <input
                                type="text"
                                value={editAccountLabel}
                                onChange={(e) => setEditAccountLabel(e.target.value)}
                                className="w-full px-3 py-2 border rounded-lg"
                                disabled={isRunning}
                              />
                            </div>
                            <div>
                              <label className="block text-xs font-medium text-gray-700 mb-1">Email</label>
                              <input
                                type="email"
                                value={editAccountEmail}
                                onChange={(e) => setEditAccountEmail(e.target.value)}
                                className="w-full px-3 py-2 border rounded-lg"
                                disabled={isRunning}
                              />
                            </div>
                            <div>
                              <label className="block text-xs font-medium text-gray-700 mb-1">Password</label>
                              <input
                                type="password"
                                value={editAccountPassword}
                                onChange={(e) => setEditAccountPassword(e.target.value)}
                                placeholder="Leave blank to keep current"
                                className="w-full px-3 py-2 border rounded-lg"
                                disabled={isRunning}
                              />
                            </div>
                            <div>
                              <label className="block text-xs font-medium text-gray-700 mb-1">
                                API Key <span className="text-gray-400 font-normal">(optional)</span>
                              </label>
                              <input
                                type="text"
                                value={editAccountApiKey}
                                onChange={(e) => setEditAccountApiKey(e.target.value)}
                                placeholder="sk_..."
                                className="w-full px-3 py-2 border rounded-lg font-mono text-sm"
                                disabled={isRunning}
                              />
                              <p className="text-xs text-gray-500 mt-1">
                                Leave blank to keep the saved API key as-is.
                              </p>
                            </div>
                            <div className="flex gap-2">
                              <button
                                onClick={updateAccount}
                                disabled={savingAccount || isRunning}
                                className="flex-1 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:bg-gray-400"
                              >
                                {savingAccount ? "Saving..." : "Save Changes"}
                              </button>
                              <button
                                onClick={cancelEditAccount}
                                disabled={savingAccount}
                                className="px-4 py-2 border rounded-lg text-gray-700 hover:bg-gray-100"
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="flex items-center justify-between text-sm p-2">
                            <div className="flex items-center gap-2">
                              <span>{acc.label} - {acc.email}</span>
                              {acc.has_api_key && (
                                <span className="inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium bg-green-100 text-green-700">
                                  🔑 API Key
                                </span>
                              )}
                            </div>
                            <div className="flex items-center gap-3">
                              <button
                                onClick={() => startEditAccount(acc)}
                                className="text-blue-600 hover:text-blue-700 text-xs"
                              >
                                Edit
                              </button>
                              <button
                                onClick={() => deleteAccount(acc.id)}
                                className="text-red-600 hover:text-red-700 text-xs"
                              >
                                Delete
                              </button>
                            </div>
                          </div>
                        )}
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

        </div>
      )}

      {/* Smartlead-specific configuration */}
      {sequencer === "smartlead" && (
        <div className="rounded-lg border bg-white p-4 space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Smartlead Configuration
            </label>
            <div className="space-y-3">
              <div>
                <label className="block text-xs font-medium text-gray-700 mb-1">
                  API Key (optional)
                </label>
                <input
                  type="text"
                  value={smartleadApiKey}
                  onChange={(e) => setSmartleadApiKey(e.target.value)}
                  placeholder="sk_..."
                  className="w-full px-3 py-2 border rounded-lg font-mono text-sm"
                  disabled={isRunning}
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-700 mb-1">
                  OAuth URL * <span className="text-gray-500 font-normal">(from Smartlead dashboard)</span>
                </label>
                <input
                  type="url"
                  value={smartleadOAuthUrl}
                  onChange={(e) => setSmartleadOAuthUrl(e.target.value)}
                  placeholder="https://server.smartlead.ai/api/v1/..."
                  className="w-full px-3 py-2 border rounded-lg font-mono text-xs"
                  disabled={isRunning}
                />
                <p className="text-xs text-gray-500 mt-1">
                  Get this URL from Smartlead → Settings → Email Accounts → Add Microsoft Account
                </p>
              </div>

              {/* Settings configuration */}
              <div className="pt-3 border-t">
                <label className="flex items-center mb-3">
                  <input
                    type="checkbox"
                    checked={smartleadSettingsEnabled}
                    onChange={(e) => setConfigureSettings(e.target.checked)}
                    disabled={isRunning || !hasSmartleadApiKey}
                    className="mr-2"
                  />
                  <span className="text-sm font-medium text-gray-700">Configure sending & warmup settings</span>
                </label>

                {smartleadSettingsEnabled && (
                  <div className="grid grid-cols-2 gap-3 pl-6">
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">Max emails/day</label>
                      <input
                        type="number"
                        value={maxEmailPerDay}
                        onChange={(e) => setMaxEmailPerDay(parseInt(e.target.value))}
                        className="w-full px-2 py-1 border rounded text-sm"
                        disabled={isRunning}
                        min={1}
                        max={50}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">Wait mins</label>
                      <input
                        type="number"
                        value={waitMins}
                        onChange={(e) => setWaitMins(parseInt(e.target.value))}
                        className="w-full px-2 py-1 border rounded text-sm"
                        disabled={isRunning}
                        min={1}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">Warmup/day</label>
                      <input
                        type="number"
                        value={warmupPerDay}
                        onChange={(e) => setWarmupPerDay(parseInt(e.target.value))}
                        className="w-full px-2 py-1 border rounded text-sm"
                        disabled={isRunning}
                        min={1}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">Rampup</label>
                      <input
                        type="number"
                        value={rampup}
                        onChange={(e) => setRampup(parseInt(e.target.value))}
                        className="w-full px-2 py-1 border rounded text-sm"
                        disabled={isRunning}
                        min={1}
                      />
                    </div>
                    <div>
                      <label className="block text-xs text-gray-600 mb-1">Reply rate %</label>
                      <input
                        type="number"
                        value={replyRate}
                        onChange={(e) => setReplyRate(parseInt(e.target.value))}
                        className="w-full px-2 py-1 border rounded text-sm"
                        disabled={isRunning}
                        min={0}
                        max={100}
                      />
                    </div>
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Shared Configuration Options */}
      <div className="rounded-lg border bg-white p-4 space-y-3">
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
            Each worker runs one browser session. 3 is conservative; 5 is the Railway max for large uploads.
          </p>
        </div>

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

      {/* Info banner */}
      <div className="rounded-lg border border-blue-200 bg-blue-50 p-4 text-sm text-blue-700">
        <p className="font-medium">What this does:</p>
        <p className="mt-1">
          1) {sequencer === "instantly" ? "Logs into your Instantly.ai account" : "Uses Smartlead OAuth URL"} using Selenium WebDriver
        </p>
        <p className="mt-1">
          2) {sequencer === "instantly" ? "Navigates to the email accounts page and uploads each mailbox" : "Processes Microsoft OAuth for each mailbox"}
        </p>
        <p className="mt-1">
          3) Uses parallel workers to process multiple mailboxes simultaneously
        </p>
        {sequencer === "smartlead" && smartleadSettingsEnabled && (
          <p className="mt-1">
            4) Configures sending limits and warmup settings via Smartlead API
          </p>
        )}
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
            All {status.total} mailboxes have been successfully uploaded to {sequencer === "instantly" ? "Instantly.ai" : "Smartlead.ai"}.
            You can now manage them from your {sequencer === "instantly" ? "Instantly" : "Smartlead"} dashboard.
          </p>
        </div>
      )}
    </div>
  );
}
