"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";

interface CsvUploadJob {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  sequencer: string;
  total: number;
  uploaded: number;
  failed: number;
  skipped: number;
  errors: string[];
  results: { email: string; status: string; error?: string }[];
  created_at: string;
}

interface InstantlyAccount {
  id: string;
  label: string;
  email: string;
  has_api_key: boolean;
  is_default: boolean;
}

export default function CsvSequencerUpload() {
  const [files, setFiles] = useState<File[]>([]);
  const [sequencer, setSequencer] = useState<"instantly" | "smartlead">("instantly");
  const [accounts, setAccounts] = useState<InstantlyAccount[]>([]);
  const [useExistingAccount, setUseExistingAccount] = useState(true);
  const [selectedAccountId, setSelectedAccountId] = useState("");
  const [instantlyEmail, setInstantlyEmail] = useState("");
  const [instantlyPassword, setInstantlyPassword] = useState("");
  const [smartleadApiKey, setSmartleadApiKey] = useState("");
  const [smartleadOAuthUrl, setSmartleadOAuthUrl] = useState("");
  const [configureSettings, setConfigureSettings] = useState(true);
  const [maxEmailPerDay, setMaxEmailPerDay] = useState(6);
  const [waitMins, setWaitMins] = useState(60);
  const [warmupPerDay, setWarmupPerDay] = useState(40);
  const [rampup, setRampup] = useState(1);
  const [replyRate, setReplyRate] = useState(79);
  const [numWorkers, setNumWorkers] = useState(2);
  const [skipExisting, setSkipExisting] = useState(true);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<CsvUploadJob | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [lostJob, setLostJob] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const notFoundCountRef = useRef(0);

  const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

  const fetchAccounts = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/v1/wizard/instantly/accounts`);
      if (res.ok) {
        const data = await res.json();
        setAccounts(data.accounts || []);
        const def = data.accounts?.find((a: InstantlyAccount) => a.is_default);
        if (def && !selectedAccountId) setSelectedAccountId(def.id);
      }
    } catch (_) {}
  }, [API_BASE, selectedAccountId]);

  useEffect(() => {
    if (sequencer === "instantly") fetchAccounts();
  }, [sequencer, fetchAccounts]);

  useEffect(() => {
    try {
      const k = localStorage.getItem("smartlead_api_key");
      const u = localStorage.getItem("smartlead_oauth_url");
      if (k) setSmartleadApiKey(k);
      if (u) setSmartleadOAuthUrl(u);
    } catch (_) {}
  }, []);

  // Poll job status
  useEffect(() => {
    if (!jobId || !isRunning) return;
    notFoundCountRef.current = 0;
    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/v1/wizard/sequencer/csv-upload/${jobId}/status`);
        if (res.ok) {
          notFoundCountRef.current = 0;
          const data = await res.json();
          setJob(data);
          if (data.status === "completed" || data.status === "failed") {
            setIsRunning(false);
          }
        } else if (res.status === 404) {
          notFoundCountRef.current += 1;
          // After 3 consecutive 404s (15s), the job is truly gone (e.g. server restarted)
          if (notFoundCountRef.current >= 3) {
            setIsRunning(false);
            setLostJob(true);
            setError("Upload job was lost — the server may have restarted. Please start a new upload.");
          }
        }
      } catch (_) {}
    };
    poll();
    const iv = setInterval(poll, 5000);
    return () => clearInterval(iv);
  }, [jobId, isRunning, API_BASE]);

  const handleFiles = (newFiles: FileList | File[]) => {
    const csvFiles = Array.from(newFiles).filter(
      (f) => f.name.endsWith(".csv") || f.type === "text/csv"
    );
    if (csvFiles.length === 0) {
      setError("Please select CSV files only");
      return;
    }
    setFiles((prev) => {
      const names = new Set(prev.map((f) => f.name));
      const unique = csvFiles.filter((f) => !names.has(f.name));
      return [...prev, ...unique];
    });
    setError(null);
  };

  const removeFile = (name: string) => {
    setFiles((prev) => prev.filter((f) => f.name !== name));
  };

  const startUpload = async () => {
    if (files.length === 0) { setError("Please add at least one CSV file"); return; }
    setError(null);

    const formData = new FormData();
    files.forEach((f) => formData.append("files", f));
    formData.append("sequencer", sequencer);
    formData.append("num_workers", String(numWorkers));
    formData.append("skip_existing", String(skipExisting));

    if (sequencer === "instantly") {
      if (useExistingAccount) {
        if (!selectedAccountId) { setError("Please select an Instantly account"); return; }
        formData.append("account_id", selectedAccountId);
      } else {
        if (!instantlyEmail || !instantlyPassword) { setError("Enter Instantly credentials"); return; }
        formData.append("instantly_email", instantlyEmail);
        formData.append("instantly_password", instantlyPassword);
      }
    } else {
      if (!smartleadApiKey || !smartleadOAuthUrl) { setError("Enter Smartlead API key and OAuth URL"); return; }
      formData.append("smartlead_api_key", smartleadApiKey);
      formData.append("smartlead_oauth_url", smartleadOAuthUrl);
      formData.append("configure_settings", String(configureSettings));
      formData.append("max_email_per_day", String(maxEmailPerDay));
      formData.append("time_to_wait_in_mins", String(waitMins));
      formData.append("total_warmup_per_day", String(warmupPerDay));
      formData.append("daily_rampup", String(rampup));
      formData.append("reply_rate_percentage", String(replyRate));
      try {
        localStorage.setItem("smartlead_api_key", smartleadApiKey);
        localStorage.setItem("smartlead_oauth_url", smartleadOAuthUrl);
      } catch (_) {}
    }

    setIsRunning(true);
    try {
      const res = await fetch(`${API_BASE}/api/v1/wizard/sequencer/csv-upload`, {
        method: "POST",
        body: formData,
      });
      const data = await res.json();
      if (data.job_id) {
        setJobId(data.job_id);
        setJob({ job_id: data.job_id, status: "running", sequencer, total: data.total_mailboxes || 0, uploaded: 0, failed: 0, skipped: 0, errors: [], results: [], created_at: new Date().toISOString() });
      } else {
        setError(data.detail || data.message || "Failed to start upload");
        setIsRunning(false);
      }
    } catch (err) {
      setError("Network error starting upload");
      setIsRunning(false);
    }
  };

  const resetForm = () => {
    setFiles([]);
    setJobId(null);
    setJob(null);
    setIsRunning(false);
    setError(null);
    setLostJob(false);
    notFoundCountRef.current = 0;
  };

  const allComplete = job && job.status === "completed";
  const progress = job && job.total > 0 ? Math.round(((job.uploaded + job.failed + job.skipped) / job.total) * 100) : 0;

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold text-gray-900">CSV Upload to Sequencer</h2>
        <p className="mt-1 text-sm text-gray-500">Upload mailboxes from CSV files directly to your sequencer. CSV must have <code className="bg-gray-100 px-1 rounded">email</code> and <code className="bg-gray-100 px-1 rounded">password</code> columns.</p>
      </div>

      {/* File Drop Zone */}
      {!jobId && (
        <div
          className={`border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer ${dragOver ? "border-blue-500 bg-blue-50" : "border-gray-300 bg-gray-50 hover:border-gray-400"}`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); handleFiles(e.dataTransfer.files); }}
          onClick={() => fileInputRef.current?.click()}
        >
          <input ref={fileInputRef} type="file" multiple accept=".csv" className="hidden" onChange={(e) => { if (e.target.files) handleFiles(e.target.files); e.target.value = ""; }} />
          <div className="text-4xl mb-2">📁</div>
          <p className="text-sm text-gray-600">Drag & drop CSV files here, or <span className="text-blue-600 font-medium">click to browse</span></p>
          <p className="text-xs text-gray-400 mt-1">Multiple CSV files supported. Required columns: email, password</p>
        </div>
      )}

      {/* File List */}
      {files.length > 0 && !jobId && (
        <div className="rounded-lg border bg-white p-4">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-medium text-gray-700">{files.length} file{files.length !== 1 ? "s" : ""} selected</h3>
            <button onClick={() => setFiles([])} className="text-xs text-red-600 hover:text-red-700">Clear all</button>
          </div>
          <div className="space-y-2 max-h-40 overflow-y-auto">
            {files.map((f) => (
              <div key={f.name} className="flex items-center justify-between text-sm bg-gray-50 rounded px-3 py-2">
                <span className="truncate">📄 {f.name} <span className="text-gray-400">({(f.size / 1024).toFixed(1)} KB)</span></span>
                <button onClick={() => removeFile(f.name)} className="text-red-500 hover:text-red-700 ml-2">✕</button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Sequencer Selection */}
      {!jobId && (
        <>
          <div className="rounded-lg border bg-white p-4">
            <label className="block text-sm font-medium text-gray-700 mb-2">Select Sequencer</label>
            <div className="flex gap-4">
              <label className="flex items-center">
                <input type="radio" checked={sequencer === "instantly"} onChange={() => setSequencer("instantly")} disabled={isRunning} className="mr-2" />
                <span className="text-sm text-gray-700">⚡ Instantly.ai</span>
              </label>
              <label className="flex items-center">
                <input type="radio" checked={sequencer === "smartlead"} onChange={() => setSequencer("smartlead")} disabled={isRunning} className="mr-2" />
                <span className="text-sm text-gray-700">🚀 Smartlead.ai</span>
              </label>
            </div>
          </div>

          {/* Instantly Config */}
          {sequencer === "instantly" && (
            <div className="rounded-lg border bg-white p-4 space-y-4">
              <label className="block text-sm font-medium text-gray-700 mb-2">Instantly.ai Account</label>
              <div className="flex gap-4 mb-3">
                <label className="flex items-center"><input type="radio" checked={useExistingAccount} onChange={() => setUseExistingAccount(true)} className="mr-2" />Use Saved Account</label>
                <label className="flex items-center"><input type="radio" checked={!useExistingAccount} onChange={() => setUseExistingAccount(false)} className="mr-2" />Enter Credentials</label>
              </div>
              {useExistingAccount ? (
                <select value={selectedAccountId} onChange={(e) => setSelectedAccountId(e.target.value)} disabled={isRunning} className="w-full px-3 py-2 border rounded-lg bg-white">
                  <option value="">-- Select Account --</option>
                  {accounts.map((a) => (<option key={a.id} value={a.id}>{a.label} ({a.email}){a.is_default ? " - Default" : ""}</option>))}
                </select>
              ) : (
                <div className="space-y-3">
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">Email</label>
                    <input type="email" value={instantlyEmail} onChange={(e) => setInstantlyEmail(e.target.value)} placeholder="your@instantly.ai" className="w-full px-3 py-2 border rounded-lg" disabled={isRunning} />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-700 mb-1">Password</label>
                    <input type="password" value={instantlyPassword} onChange={(e) => setInstantlyPassword(e.target.value)} placeholder="Password" className="w-full px-3 py-2 border rounded-lg" disabled={isRunning} />
                  </div>
                </div>
              )}
            </div>
          )}

          {/* Smartlead Config */}
          {sequencer === "smartlead" && (
            <div className="rounded-lg border bg-white p-4 space-y-4">
              <label className="block text-sm font-medium text-gray-700 mb-2">Smartlead Configuration</label>
              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-medium text-gray-700 mb-1">API Key *</label>
                  <input type="text" value={smartleadApiKey} onChange={(e) => setSmartleadApiKey(e.target.value)} placeholder="sk_..." className="w-full px-3 py-2 border rounded-lg font-mono text-sm" disabled={isRunning} />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-700 mb-1">OAuth URL *</label>
                  <input type="url" value={smartleadOAuthUrl} onChange={(e) => setSmartleadOAuthUrl(e.target.value)} placeholder="https://server.smartlead.ai/api/v1/..." className="w-full px-3 py-2 border rounded-lg font-mono text-xs" disabled={isRunning} />
                </div>
                <div className="pt-3 border-t">
                  <label className="flex items-center mb-3">
                    <input type="checkbox" checked={configureSettings} onChange={(e) => setConfigureSettings(e.target.checked)} disabled={isRunning} className="mr-2" />
                    <span className="text-sm font-medium text-gray-700">Configure sending & warmup settings</span>
                  </label>
                  {configureSettings && (
                    <div className="grid grid-cols-2 gap-3 pl-6">
                      <div><label className="block text-xs text-gray-600 mb-1">Max emails/day</label><input type="number" value={maxEmailPerDay} onChange={(e) => setMaxEmailPerDay(parseInt(e.target.value))} className="w-full px-2 py-1 border rounded text-sm" min={1} max={50} /></div>
                      <div><label className="block text-xs text-gray-600 mb-1">Wait mins</label><input type="number" value={waitMins} onChange={(e) => setWaitMins(parseInt(e.target.value))} className="w-full px-2 py-1 border rounded text-sm" min={1} /></div>
                      <div><label className="block text-xs text-gray-600 mb-1">Warmup/day</label><input type="number" value={warmupPerDay} onChange={(e) => setWarmupPerDay(parseInt(e.target.value))} className="w-full px-2 py-1 border rounded text-sm" min={1} /></div>
                      <div><label className="block text-xs text-gray-600 mb-1">Rampup</label><input type="number" value={rampup} onChange={(e) => setRampup(parseInt(e.target.value))} className="w-full px-2 py-1 border rounded text-sm" min={1} /></div>
                      <div><label className="block text-xs text-gray-600 mb-1">Reply rate %</label><input type="number" value={replyRate} onChange={(e) => setReplyRate(parseInt(e.target.value))} className="w-full px-2 py-1 border rounded text-sm" min={0} max={100} /></div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Workers */}
          <div className="rounded-lg border bg-white p-4 space-y-3">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">Parallel Workers: {numWorkers}</label>
              <input type="range" min="1" max="3" value={numWorkers} onChange={(e) => setNumWorkers(parseInt(e.target.value))} disabled={isRunning} className="w-full" />
              <p className="text-xs text-gray-500 mt-1">Each worker uses ~200MB RAM. 2 recommended.</p>
            </div>
            <label className="flex items-center">
              <input type="checkbox" checked={skipExisting} onChange={(e) => setSkipExisting(e.target.checked)} disabled={isRunning} className="mr-2" />
              <span className="text-sm text-gray-700">Skip already-uploaded emails (dedup)</span>
            </label>
          </div>

          {/* Start Button */}
          <button onClick={startUpload} disabled={isRunning || files.length === 0} className={`w-full px-6 py-3 rounded-lg font-medium text-white transition-colors ${isRunning || files.length === 0 ? "bg-gray-400 cursor-not-allowed" : "bg-blue-600 hover:bg-blue-700"}`}>
            {isRunning ? (
              <span className="flex items-center justify-center gap-2">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
                Starting Upload...
              </span>
            ) : `Upload ${files.length} CSV${files.length !== 1 ? "s" : ""} to ${sequencer === "instantly" ? "Instantly" : "Smartlead"}`}
          </button>
        </>
      )}

      {/* Job Status */}
      {job && (
        <>
          <div className="grid grid-cols-4 gap-4">
            <div className="rounded-lg border p-4 text-center"><p className="text-2xl font-bold text-gray-900">{job.total}</p><p className="text-sm text-gray-500">Total</p></div>
            <div className="rounded-lg border p-4 text-center"><p className="text-2xl font-bold text-green-600">{job.uploaded}</p><p className="text-sm text-gray-500">Uploaded</p></div>
            <div className="rounded-lg border p-4 text-center"><p className="text-2xl font-bold text-yellow-600">{job.skipped}</p><p className="text-sm text-gray-500">Skipped</p></div>
            <div className="rounded-lg border p-4 text-center"><p className="text-2xl font-bold text-red-600">{job.failed}</p><p className="text-sm text-gray-500">Failed</p></div>
          </div>

          {job.total > 0 && (
            <div className="w-full bg-gray-200 rounded-full h-3">
              <div className={`h-3 rounded-full transition-all duration-500 ${allComplete ? "bg-green-500" : "bg-blue-500"}`} style={{ width: `${progress}%` }} />
            </div>
          )}

          {isRunning && (
            <div className="flex items-center gap-2 text-sm text-blue-600">
              <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" /><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" /></svg>
              Upload in progress... polling every 5s
            </div>
          )}

          {allComplete && (
            <div className="rounded-lg border border-green-200 bg-green-50 p-4">
              <p className="text-lg font-semibold text-green-700">Upload Complete! 🎉</p>
              <p className="mt-1 text-sm text-green-600">{job.uploaded} mailboxes uploaded to {job.sequencer === "instantly" ? "Instantly.ai" : "Smartlead.ai"}.{job.skipped > 0 && ` ${job.skipped} skipped (duplicates).`}{job.failed > 0 && ` ${job.failed} failed.`}</p>
            </div>
          )}

          {job.status === "failed" && !lostJob && (
            <div className="rounded-lg border border-red-200 bg-red-50 p-4">
              <p className="text-lg font-semibold text-red-700">Upload Failed</p>
              <p className="mt-1 text-sm text-red-600">{job.errors?.join(", ") || "An error occurred during upload."}</p>
            </div>
          )}

          {lostJob && (
            <div className="rounded-lg border border-amber-300 bg-amber-50 p-4">
              <p className="text-lg font-semibold text-amber-700">⚠️ Upload Job Lost</p>
              <p className="mt-1 text-sm text-amber-600">The server appears to have restarted and the upload job tracking was lost. The upload may have partially completed — enable &quot;Skip already-uploaded emails&quot; when re-uploading to avoid duplicates.</p>
            </div>
          )}

          {/* Results Table */}
          {job.results && job.results.length > 0 && (
            <div className="rounded-lg border bg-white overflow-hidden">
              <div className="px-4 py-3 bg-gray-50 border-b"><h3 className="text-sm font-medium text-gray-700">Results ({job.results.length})</h3></div>
              <div className="max-h-64 overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="bg-gray-50 sticky top-0"><tr><th className="text-left px-4 py-2 text-gray-600">Email</th><th className="text-left px-4 py-2 text-gray-600">Status</th><th className="text-left px-4 py-2 text-gray-600">Error</th></tr></thead>
                  <tbody className="divide-y">
                    {job.results.map((r, i) => (
                      <tr key={i} className="hover:bg-gray-50">
                        <td className="px-4 py-2 font-mono text-xs">{r.email}</td>
                        <td className="px-4 py-2">
                          <span className={`inline-flex px-2 py-0.5 rounded text-xs font-medium ${r.status === "uploaded" ? "bg-green-100 text-green-700" : r.status === "skipped" ? "bg-yellow-100 text-yellow-700" : "bg-red-100 text-red-700"}`}>{r.status}</span>
                        </td>
                        <td className="px-4 py-2 text-xs text-red-600">{r.error || ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* Reset */}
          {!isRunning && (
            <button onClick={resetForm} className="px-6 py-2.5 rounded-lg font-medium bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors">
              ← Upload More CSVs
            </button>
          )}
        </>
      )}

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">{error}</div>
      )}
    </div>
  );
}
