"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Check = {
  tenant_id: string; tenant_number: string; admin: string; status: string;
  stage?: string; detail?: string; ndr_code?: string; sender?: string;
  sent_at?: string; browser_available: boolean;
};
type Results = { checks: Check[]; counts: Record<string, number>; parallel_browsers: number; recipient: string };
const api = "/api/readiness";
const terminal = new Set(["no_ndr_observed", "other_ndr", "blocked_5.7.705", "blocked_5.7.750", "skipped_by_user"]);

function statusLabel(status: string) {
  return ({ no_ndr_observed: "No bounce observed", other_ndr: "Bounced", needs_user: "Needs attention", error: "Failed", skipped_by_user: "Skipped", awaiting_user: "Waiting for you", queued: "Queued", running: "Running", monitoring: "Watching for bounces", send_uncertain: "Send unconfirmed" } as Record<string, string>)[status] || status.replaceAll("_", " ");
}

async function request(path: string, options?: RequestInit) {
  const response = await fetch(`${api}/${path}`, { cache: "no-store", ...options });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Request failed (${response.status}).`);
  }
  return response;
}

export default function TenantReadiness() {
  const [data, setData] = useState<Results | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [browser, setBrowser] = useState<Check | null>(null);
  const [image, setImage] = useState("");
  const [typed, setTyped] = useState("");
  const [browserMessage, setBrowserMessage] = useState("");
  const [inputBusy, setInputBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const imageURL = useRef("");
  const dialogRef = useRef<HTMLDialogElement>(null);

  const refresh = useCallback(async () => {
    const response = await fetch(`${api}/checks`, { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load the latest checks. They continue running on the server.");
    setData(await response.json());
  }, []);

  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname);
    refresh().catch(e => setError(e.message));
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => { refresh().catch(e => setError(e.message)); }, 5000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!browser) return;
    dialogRef.current?.showModal();
    let cancelled = false;
    let fetching = false;
    const updateImage = async () => {
      if (fetching) return;
      fetching = true;
      try {
        const response = await request(`${browser.tenant_id}/browser`);
        const blob = await response.blob();
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        const old = imageURL.current;
        imageURL.current = url;
        setImage(url);
        setBrowserMessage("");
        if (old) URL.revokeObjectURL(old);
      } catch (e) {
        if (!cancelled) setBrowserMessage((e as Error).message);
      } finally { fetching = false; }
    };
    void updateImage();
    const timer = window.setInterval(updateImage, 1500);
    return () => {
      cancelled = true; window.clearInterval(timer);
      if (imageURL.current) URL.revokeObjectURL(imageURL.current);
      imageURL.current = "";
      setImage("");
    };
  }, [browser]);

  async function action(payload: Record<string, unknown>) {
    if (!browser || inputBusy) return;
    setInputBusy(true);
    try {
      await request(`${browser.tenant_id}/browser`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      setTyped("");
    } catch (e) { setBrowserMessage((e as Error).message); }
    finally { setInputBusy(false); }
  }

  async function upload(file: File) {
    setBusy(true); setError("");
    try {
      const body = new FormData(); body.append("file", file);
      await request("upload", { method: "POST", body });
      await refresh();
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); if (inputRef.current) inputRef.current.value = ""; }
  }

  const checks = data?.checks || [];
  const passed = checks.filter(c => c.status === "no_ndr_observed").length;
  const bounced = checks.filter(c => c.status === "other_ndr" || c.status.startsWith("blocked_")).length;
  const active = checks.filter(c => ["running", "monitoring", "awaiting_user"].includes(c.status)).length;
  const attention = checks.filter(c => ["needs_user", "error", "send_uncertain"].includes(c.status)).length;

  return <main className="space-y-6 p-6">
    <div><h1 className="text-2xl font-semibold text-gray-900">Tenant readiness</h1>
      <p className="mt-2 text-sm text-gray-600">Activate an eligible free trial, license a mailbox and check outbound mail. Checks continue on the server while this app is closed.</p></div>
    {error && <p role="alert" className="rounded-lg bg-red-50 p-3 text-sm text-red-800">{error}</p>}
    {!data && <p role="status" className="text-sm text-gray-500">Loading tenant progress…</p>}
    <>
        <div className="flex flex-wrap items-center gap-3">
          <input ref={inputRef} type="file" accept=".csv,text/csv" className="hidden" aria-label="Upload tenant CSV" onChange={e => { const file = e.target.files?.[0]; if (file) void upload(file); }} />
          <button disabled={busy} onClick={() => inputRef.current?.click()} className="rounded bg-blue-600 px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? "Importing…" : "Upload CSV and start"}</button>
          <a href={`${api}/report.csv`} className="rounded border px-4 py-2 text-sm">Download report</a>
          <span className="text-sm text-gray-500">Recipient: {data?.recipient} · Up to {data?.parallel_browsers} concurrent browsers</span>
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-5">{[["No bounce observed", passed], ["Bounced", bounced], ["Active", active], ["Queued", data?.counts.queued || 0], ["Needs attention", attention]].map(([label, count]) => <div key={label} className="rounded-lg border bg-white p-4"><p className="text-sm text-gray-500">{label}</p><p className="mt-1 text-2xl font-semibold">{count}</p></div>)}</div>
        <p className="text-sm text-gray-600">Each tenant sends one test. “No bounce observed” means the send was accepted and no matching delivery failure arrived during five minutes of successful mailbox checks. Trials use the shortest offered term; renewal settings stay unchanged.</p>
        <div className="overflow-x-auto rounded-lg border bg-white"><table className="w-full text-left text-sm"><thead className="border-b bg-gray-50"><tr>{["Tenant", "Result", "Progress", "Details", "Action"].map(h => <th key={h} className="p-3 font-medium">{h}</th>)}</tr></thead><tbody>
          {checks.length === 0 && <tr><td colSpan={5} className="p-6 text-gray-500">Upload the tenant CSV to start checking.</td></tr>}
          {checks.map(check => <tr key={check.tenant_id} className="border-b align-top last:border-0">
            <td className="p-3"><p className="font-medium">{check.admin}</p><p className="mt-1 text-xs text-gray-500">{check.tenant_id}</p></td>
            <td className="p-3"><span className={`inline-block rounded px-2 py-1 ${check.status === "no_ndr_observed" ? "bg-green-50 text-green-800" : check.status === "other_ndr" || check.status.startsWith("blocked_") ? "bg-red-50 text-red-800" : "bg-gray-100 text-gray-700"}`}>{statusLabel(check.status)}</span>{check.ndr_code && <p className="mt-1 font-mono">{check.ndr_code}</p>}</td>
            <td className="p-3 text-gray-600">{check.stage?.replaceAll("_", " ")}</td>
            <td className="max-w-sm p-3 text-gray-600">{check.detail || (check.sent_at ? `Test sent from ${check.sender}` : "")}</td>
            <td className="p-3">{check.browser_available ? <button className="rounded border px-3 py-1" onClick={() => setBrowser(check)}>Open sign-in</button> : !terminal.has(check.status) && !["running", "monitoring", "awaiting_user", "queued"].includes(check.status) ? <button className="rounded border px-3 py-1" onClick={async () => {
              try { await request(`${check.tenant_id}/retry`, { method: "POST" }); await refresh(); } catch (e) { setError((e as Error).message); }
            }}>Retry</button> : null}</td>
          </tr>)}
        </tbody></table></div>
      </>
    {browser && <dialog ref={dialogRef} onCancel={() => setBrowser(null)} className="m-auto w-[min(95vw,1500px)] max-w-none rounded-xl p-4 backdrop:bg-black/50">
      <div className="mb-3 flex items-center justify-between gap-3"><div><h2 className="font-semibold">Microsoft sign-in · {browser.admin}</h2><p className="text-sm text-gray-500">Click the Microsoft page, then type into the selected field below.</p></div><button autoFocus onClick={() => setBrowser(null)} className="rounded border px-3 py-1">Close</button></div>
      {browserMessage && <p role="status" className="mb-3 text-sm text-amber-800">{browserMessage}</p>}
      <div className="max-h-[65vh] overflow-auto rounded border bg-gray-50">{image ? <img src={image} alt="Live Microsoft browser for this tenant" className="h-auto w-full cursor-pointer" draggable={false} onClick={e => {
        const bounds = e.currentTarget.getBoundingClientRect();
        void action({ op: "click", x: (e.clientX - bounds.left) * 1440 / bounds.width, y: (e.clientY - bounds.top) * 1000 / bounds.height });
      }} /> : <p className="p-8">Connecting to the tenant browser…</p>}</div>
      <form className="mt-3 flex flex-wrap gap-2" onSubmit={e => { e.preventDefault(); void action({ op: "text", text: typed }); }}>
        <label htmlFor="browser-input" className="sr-only">Text or authenticator code for the selected Microsoft field</label><input id="browser-input" type="password" autoComplete="off" value={typed} onChange={e => setTyped(e.target.value)} placeholder="Text or authenticator code" className="min-w-48 flex-1 rounded border p-2" />
        <button disabled={inputBusy || !typed} className="rounded border px-3 py-2 disabled:opacity-50">Type into field</button>
        {[["Tab", "Tab"], ["Enter", "Enter"], ["Select all", "Control+A"]].map(([label, key]) => <button key={key} type="button" disabled={inputBusy} onClick={() => void action({ op: "key", key })} className="rounded border px-3 py-2">{label}</button>)}
        <button type="button" disabled={inputBusy} onClick={() => void action({ op: "continue" })} className="rounded bg-blue-600 px-3 py-2 text-white">Resume automation</button>
      </form>
    </dialog>}
  </main>;
}
