"use client";

import { useState, useMemo } from "react";

export interface NSGroup {
  nameservers: string[];
  domains: string[];
  count: number;
}

interface Props {
  groups: NSGroup[];
}

const COPY_LABELS: Record<string, string> = {
  ns: "Copy NS",
  domains: "Copy Domains",
  table: "Copy Domain + NS Table (TSV)",
  all: "Copy ALL Domains (across groups)",
};

function GroupCard({ group, idx }: { group: NSGroup; idx: number }) {
  const [filter, setFilter] = useState("");
  const [copied, setCopied] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return group.domains;
    return group.domains.filter((d) => d.toLowerCase().includes(q));
  }, [filter, group.domains]);

  const copy = async (key: "ns" | "domains" | "table", text: string, msg: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(`${key}:${msg}`);
      setTimeout(() => setCopied(null), 2500);
    } catch (err) {
      console.error("Clipboard failed:", err);
    }
  };

  const nsText = group.nameservers.join("\n");
  const domainsText = group.domains.join("\n");
  const tableText = group.domains
    .map((d) => [d, ...group.nameservers].join("\t"))
    .join("\n");

  return (
    <div className="bg-white rounded-lg border border-yellow-200 overflow-hidden mb-4">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 bg-yellow-100/60 border-b border-yellow-200">
        <div className="text-sm font-semibold text-yellow-900">
          Group {idx + 1} · {group.count} domain{group.count !== 1 ? "s" : ""}
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => copy("ns", nsText, "Nameservers")}
            className="px-3 py-1 text-xs font-medium rounded bg-blue-600 text-white hover:bg-blue-700"
          >
            {COPY_LABELS.ns}
          </button>
          <button
            onClick={() => copy("domains", domainsText, `${group.domains.length} domains`)}
            className="px-3 py-1 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700"
          >
            {COPY_LABELS.domains} ({group.domains.length})
          </button>
          <button
            onClick={() => copy("table", tableText, `${group.domains.length}-row table`)}
            className="px-3 py-1 text-xs font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700"
            title="Tab-separated: domain TAB ns1 TAB ns2 — paste straight into Excel/Sheets"
          >
            {COPY_LABELS.table}
          </button>
        </div>
      </div>

      {copied && (
        <div className="px-4 py-2 text-xs text-green-800 bg-green-50 border-b border-green-200">
          ✓ Copied {copied.split(":")[1]} to clipboard
        </div>
      )}

      {/* Nameservers */}
      <div className="px-4 py-3 bg-blue-50/40 border-b border-blue-100">
        <div className="text-xs font-semibold text-blue-900 mb-1.5">Nameservers</div>
        <div className="space-y-1">
          {group.nameservers.map((ns, i) => (
            <div key={ns} className="flex items-center gap-2">
              <span className="inline-block w-10 text-xs font-medium text-gray-500">NS{i + 1}:</span>
              <code
                className="flex-1 bg-white border border-blue-200 px-3 py-1.5 rounded text-sm font-mono text-blue-800 cursor-pointer hover:bg-blue-50"
                onClick={() => copy("ns", ns, `NS${i + 1}`)}
                title="Click to copy this nameserver"
              >
                {ns}
              </code>
            </div>
          ))}
        </div>
      </div>

      {/* Domains list */}
      <div className="px-4 py-3">
        <div className="flex items-center justify-between mb-2">
          <div className="text-xs font-semibold text-gray-700">
            Domains ({filtered.length}{filtered.length !== group.domains.length ? ` of ${group.domains.length}` : ""})
          </div>
          <input
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter domains…"
            className="px-2 py-1 text-xs border border-gray-300 rounded w-48 focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          />
        </div>
        <div className="max-h-72 overflow-y-auto border border-gray-200 rounded bg-gray-50 divide-y divide-gray-100">
          {filtered.map((d) => (
            <div
              key={d}
              className="px-3 py-1.5 text-sm font-mono text-gray-800 hover:bg-yellow-50 cursor-pointer flex items-center justify-between group"
              onClick={() => copy("domains", d, d)}
              title="Click to copy this domain"
            >
              <span>{d}</span>
              <span className="text-[10px] text-gray-400 opacity-0 group-hover:opacity-100">click to copy</span>
            </div>
          ))}
          {filtered.length === 0 && (
            <div className="px-3 py-3 text-sm text-gray-400 text-center">No domains match filter</div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function NameserverGroupBanner({ groups }: Props) {
  const [allCopied, setAllCopied] = useState(false);

  const totalDomains = groups.reduce((sum, g) => sum + g.count, 0);
  const allDomainsText = groups.flatMap((g) => g.domains).join("\n");
  const allTableText = groups
    .flatMap((g) => g.domains.map((d) => [d, ...g.nameservers].join("\t")))
    .join("\n");

  const copyAllDomains = async () => {
    try {
      await navigator.clipboard.writeText(allDomainsText);
      setAllCopied(true);
      setTimeout(() => setAllCopied(false), 2500);
    } catch (err) {
      console.error("Clipboard failed:", err);
    }
  };

  const copyAllTable = async () => {
    try {
      await navigator.clipboard.writeText(allTableText);
      setAllCopied(true);
      setTimeout(() => setAllCopied(false), 2500);
    } catch (err) {
      console.error("Clipboard failed:", err);
    }
  };

  return (
    <div>
      {/* Top-level "Copy ALL across groups" actions */}
      <div className="flex flex-wrap gap-2 mb-3 items-center">
        <span className="text-xs text-yellow-800">
          {totalDomains} total domain{totalDomains !== 1 ? "s" : ""} across {groups.length} group{groups.length !== 1 ? "s" : ""}:
        </span>
        <button
          onClick={copyAllDomains}
          className="px-3 py-1 text-xs font-medium rounded bg-indigo-600 text-white hover:bg-indigo-700"
        >
          {COPY_LABELS.all}
        </button>
        <button
          onClick={copyAllTable}
          className="px-3 py-1 text-xs font-medium rounded bg-emerald-600 text-white hover:bg-emerald-700"
          title="domain TAB ns1 TAB ns2 — one row per domain, all groups"
        >
          Copy ALL as Table (TSV)
        </button>
        {allCopied && (
          <span className="text-xs text-green-700 font-medium">✓ Copied to clipboard</span>
        )}
      </div>

      {groups.map((g, i) => (
        <GroupCard key={`${g.nameservers.join("-")}-${i}`} group={g} idx={i} />
      ))}
    </div>
  );
}
