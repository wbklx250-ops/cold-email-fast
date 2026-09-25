"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { apiRequest, HttpError } from "@/lib/api";

const API = `${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/v1/domain-swaps`;
const STORAGE_KEY = "last-domain-swap";

type Mapping = {
  row: number;
  source: string;
  tenant_id: string;
  tenant_name: string;
  onmicrosoft_domain: string;
  old_domain: string;
  new_domain: string;
  mailbox_count: number;
  redirect_url: string | null;
  phase: string;
  batch_id: string | null;
  error: string | null;
  pipeline?: { status: string; step: number; message: string | null };
};
type Job = { id: string; name: string; status: string; created_at: string; mappings: Mapping[] };
type Problem = { row: number; source?: string; error: string };
type Preview = { valid: boolean; job?: Job; mappings: Mapping[]; errors: Problem[] };

const entries = (value: string) => value.trim() ? value.trim().split(/\r?\n/).map(line => line.trim()) : [];
function errorMessage(error: unknown): string {
  if (error instanceof HttpError) {
    const detail = (error.details as { detail?: unknown } | undefined)?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) return detail.map(item => item.msg || "Invalid input").join("; ");
  }
  return error instanceof Error ? error.message : "Request failed";
}
const labels: Record<string, string> = {
  preview: "Ready for review", queued: "Queued", running: "Running", attention: "Needs attention",
  pending: "Waiting for cleanup", removing: "Releasing licenses and removing old domain",
  removed: "Cleanup complete", provisioning: "Setting up replacement", completed: "Complete",
};

export default function DomainSwapsPage() {
  const [name, setName] = useState("Domain swap");
  const [sources, setSources] = useState("");
  const [replacements, setReplacements] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [history, setHistory] = useState<Job[]>([]);
  const [problems, setProblems] = useState<Problem[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const sourceCount = entries(sources).length;
  const targetCount = entries(replacements).length;

  const refreshHistory = useCallback(async () => {
    const data = await apiRequest<{ jobs: Job[] }>(API);
    setHistory(data.jobs);
  }, []);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiRequest<{ jobs: Job[] }>(API),
      localStorage.getItem(STORAGE_KEY)
        ? apiRequest<Job>(`${API}/${localStorage.getItem(STORAGE_KEY)}`).catch(() => null)
        : Promise.resolve(null),
    ]).then(([data, saved]) => {
      if (!cancelled) {
        setHistory(data.jobs);
        setJob(saved || data.jobs[0] || null);
      }
    }).catch(err => { if (!cancelled) setError(errorMessage(err)); });
    return () => { cancelled = true; };
  }, []);

  const jobId = job?.id;
  const shouldPoll = !!job && !["preview", "completed"].includes(job.status);
  useEffect(() => {
    if (!jobId || !shouldPoll) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const updated = await apiRequest<Job>(`${API}/${jobId}`);
        if (!cancelled) {
          setJob(updated);
          setError("");
          setHistory(old => old.map(item => item.id === updated.id ? updated : item));
        }
      } catch (err) {
        if (!cancelled) setError(`Progress could not be refreshed: ${errorMessage(err)}`);
      }
      if (!cancelled) timer = setTimeout(poll, 4000);
    };
    timer = setTimeout(poll, 2000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [jobId, shouldPoll]);

  function editInput(update: () => void) {
    update();
    setConfirmed(false);
    setProblems([]);
    if (job?.status === "preview") setJob(null);
  }

  async function preview() {
    setBusy(true); setError(""); setProblems([]); setConfirmed(false);
    try {
      const result = await apiRequest<Preview>(`${API}/preview`, {
        method: "POST", body: { name, sources: entries(sources), replacements: entries(replacements) },
      });
      setProblems(result.errors);
      setJob(result.job || null);
      if (result.job) localStorage.setItem(STORAGE_KEY, result.job.id);
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  async function act(action: "start" | "retry") {
    if (!job) return;
    setBusy(true); setError("");
    try {
      const updated = await apiRequest<Job>(`${API}/${job.id}/${action}`, { method: "POST" });
      setJob(updated);
      localStorage.setItem(STORAGE_KEY, updated.id);
      await refreshHistory();
    } catch (err) { setError(errorMessage(err)); }
    finally { setBusy(false); }
  }

  const setupRunning = job?.mappings.some(row => row.pipeline?.status === "running");
  const completed = job?.mappings.filter(row => row.phase === "completed").length || 0;
  const inputClass = "w-full rounded-lg border border-gray-300 bg-white p-3 text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 disabled:bg-gray-100";
  const buttonClass = "rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50";

  return (
    <div className="mx-auto max-w-7xl space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900">Domain swaps</h1>
        <p className="mt-2 text-sm text-gray-600">Replace domains on existing tenants, release the old users’ licenses, and run the normal setup process for the replacements.</p>
      </div>

      <section className="space-y-4 rounded-xl border border-gray-200 bg-white p-5" aria-label="New swap">
        <label className="block text-sm font-medium text-gray-800">Swap name
          <input value={name} maxLength={200} disabled={busy} onChange={e => editInput(() => setName(e.target.value))} className={`${inputClass} mt-1`} />
        </label>
        <div className="grid gap-5 md:grid-cols-2">
          <label className="block text-sm font-medium text-gray-800">Current domains or tenants · {sourceCount}
            <textarea value={sources} disabled={busy} onChange={e => editInput(() => setSources(e.target.value))} rows={8} spellCheck={false}
              placeholder={"old-domain.com\ncontoso.onmicrosoft.com\nadmin@fabrikam.onmicrosoft.com"} className={`${inputClass} mt-1 font-mono`} />
          </label>
          <label className="block text-sm font-medium text-gray-800">Replacement domains · {targetCount}
            <textarea value={replacements} disabled={busy} onChange={e => editInput(() => setReplacements(e.target.value))} rows={8} spellCheck={false}
              placeholder={"new-domain.com\nnew-domain-two.com\nnew-domain-three.com"} className={`${inputClass} mt-1 font-mono`} />
          </label>
        </div>
        <p className="text-sm text-gray-600">One entry per line, paired in the same order. Tenant names, IDs and admin emails are accepted. If a tenant has multiple domains, enter the specific current domain. Replacement domains must already be owned by you.</p>
        {sourceCount !== targetCount && <p className="text-sm text-amber-700">Both lists must have the same number of entries.</p>}
        <button className={buttonClass} disabled={busy || !sourceCount || sourceCount !== targetCount || sourceCount > 500} onClick={preview}>
          {busy ? "Working…" : "Preview mappings"}
        </button>
      </section>

      {error && <div role="alert" className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800">{error}</div>}
      {problems.length > 0 && <div role="alert" className="rounded-lg border border-amber-200 bg-amber-50 p-4">
        <p className="font-medium text-amber-900">Resolve these entries and preview again</p>
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm text-amber-900">{problems.map((problem, index) =>
          <li key={index}>{problem.row > 0 ? `Row ${problem.row}: ` : ""}{problem.source ? `${problem.source} — ` : ""}{problem.error}</li>)}</ul>
      </div>}

      {job && <section className="overflow-hidden rounded-xl border border-gray-200 bg-white" aria-label="Swap review and progress">
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 p-5">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">{job.name}</h2>
            <p className="mt-1 text-sm text-gray-600" aria-live="polite">{labels[job.status] || job.status} · {completed}/{job.mappings.length} replacements complete</p>
          </div>
          {job.status === "attention" && <button className={buttonClass} disabled={busy || setupRunning} onClick={() => act("retry")}>Retry / resume unfinished swaps</button>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-gray-600"><tr>
              <th className="px-4 py-3">Tenant</th><th className="px-4 py-3">Current → replacement</th>
              <th className="px-4 py-3">Mailboxes</th><th className="px-4 py-3">Progress</th>
            </tr></thead>
            <tbody className="divide-y divide-gray-100">{job.mappings.map(row => <tr key={row.row}>
              <td className="px-4 py-4"><Link href={`/tenants/${row.tenant_id}`} className="font-medium text-blue-700 hover:underline">{row.tenant_name}</Link><p className="mt-1 text-xs text-gray-500">{row.onmicrosoft_domain}</p></td>
              <td className="px-4 py-4"><p className="text-gray-500">{row.old_domain}</p><p className="font-medium text-gray-900">→ {row.new_domain}</p>{row.redirect_url && <p className="mt-1 max-w-xs truncate text-xs text-gray-500" title={row.redirect_url}>Redirect: {row.redirect_url}</p>}</td>
              <td className="px-4 py-4 text-gray-700">{row.mailbox_count}</td>
              <td className="max-w-md px-4 py-4 text-gray-700"><p>{labels[row.phase] || row.phase}</p>
                {row.pipeline && <p className="mt-1 text-xs">Step {row.pipeline.step}: {row.pipeline.message || row.pipeline.status}</p>}
                {row.error && <p className="mt-1 text-sm text-red-700">{row.error}</p>}
                {row.batch_id && <Link href={`/pipeline/${row.batch_id}`} className="mt-2 inline-block text-blue-700 hover:underline">Open setup / nameservers →</Link>}
              </td>
            </tr>)}</tbody>
          </table>
        </div>
        {job.status === "preview" && <div className="space-y-4 border-t border-gray-200 bg-amber-50 p-5">
          <p className="text-sm text-gray-800">The old domains will stop receiving mail on these tenants. Their licensed users will be unlicensed, Microsoft dependencies will be cleaned up, and old mailbox records will be archived. Replacement mailboxes inherit the existing names and use the normal setup credentials. New nameservers may need updating at your registrar.</p>
          <label className="flex items-start gap-3 text-sm text-gray-900">
            <input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)} className="mt-0.5 h-4 w-4" />
            I reviewed these mappings and want to remove the old domains and set up their replacements on the same tenants.
          </label>
          <button className={buttonClass} disabled={busy || !confirmed} onClick={() => act("start")}>Start {job.mappings.length} domain swap{job.mappings.length === 1 ? "" : "s"}</button>
        </div>}
        {job.status === "attention" && <p className="border-t border-gray-200 p-5 text-sm text-gray-600">Open setup to check nameservers or resolve provisioning errors, then resume. Completed cleanup is retained when retrying.</p>}
      </section>}

      {history.length > 0 && <section className="rounded-xl border border-gray-200 bg-white p-5" aria-label="Recent swaps">
        <h2 className="font-semibold text-gray-900">Recent swaps</h2>
        <div className="mt-3 divide-y divide-gray-100">{history.map(item => <button key={item.id} disabled={busy}
          onClick={() => { setJob(item); setConfirmed(false); localStorage.setItem(STORAGE_KEY, item.id); setError(""); }}
          className="flex w-full flex-wrap items-center justify-between gap-2 py-3 text-left text-sm hover:text-blue-700">
          <span>{item.name} <span className="text-gray-500">· {item.mappings.length} domains · {new Date(item.created_at).toLocaleString()}</span></span>
          <span>{labels[item.status] || item.status}</span>
        </button>)}</div>
      </section>}
    </div>
  );
}
