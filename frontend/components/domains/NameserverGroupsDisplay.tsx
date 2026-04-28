"use client";

import { useMemo, useState } from "react";
import { NameserverGroup } from "@/lib/api";

interface NameserverGroupsDisplayProps {
  groups: NameserverGroup[];
  totalDomains: number;
  onClose?: () => void;
}

interface NameserverGroupCardProps {
  group: NameserverGroup;
}

const NameserverGroupCard = ({ group }: NameserverGroupCardProps) => {
  const [filter, setFilter] = useState("");
  const [copied, setCopied] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return group.domains;
    return group.domains.filter((d) => d.toLowerCase().includes(q));
  }, [filter, group.domains]);

  const copy = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(label);
      setTimeout(() => setCopied(null), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  const nsText = group.nameservers.join("\n");
  const domainsText = group.domains.join("\n");
  const tableText = group.domains
    .map((d) => [d, ...group.nameservers].join("\t"))
    .join("\n");

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 bg-gray-50 border-b border-gray-200 flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="text-lg">📋</span>
          <span className="font-medium text-gray-900">
            Nameservers ({group.domain_count} domain{group.domain_count !== 1 ? "s" : ""})
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => copy(nsText, "ns")}
            className={`px-3 py-1 text-xs font-medium rounded border transition-colors ${
              copied === "ns"
                ? "bg-green-100 text-green-700 border-green-300"
                : "bg-blue-100 text-blue-700 hover:bg-blue-200 border-blue-300"
            }`}
          >
            {copied === "ns" ? "✓ Copied!" : "📋 Copy NS"}
          </button>
          <button
            onClick={() => copy(domainsText, "domains")}
            className={`px-3 py-1 text-xs font-medium rounded border transition-colors ${
              copied === "domains"
                ? "bg-green-100 text-green-700 border-green-300"
                : "bg-indigo-100 text-indigo-700 hover:bg-indigo-200 border-indigo-300"
            }`}
          >
            {copied === "domains" ? "✓ Copied!" : `📋 Copy Domains (${group.domains.length})`}
          </button>
          <button
            onClick={() => copy(tableText, "table")}
            title="Tab-separated: domain TAB ns1 TAB ns2 — paste into Excel/Sheets"
            className={`px-3 py-1 text-xs font-medium rounded border transition-colors ${
              copied === "table"
                ? "bg-green-100 text-green-700 border-green-300"
                : "bg-emerald-100 text-emerald-700 hover:bg-emerald-200 border-emerald-300"
            }`}
          >
            {copied === "table" ? "✓ Copied!" : "📋 Copy Table (TSV)"}
          </button>
        </div>
      </div>

      {/* Nameservers */}
      <div className="px-4 py-3 border-b border-gray-100 bg-blue-50/50">
        {group.nameservers.map((ns, index) => (
          <div key={ns} className="flex items-center gap-2 py-1">
            <span className="text-xs font-medium text-gray-500 w-8">
              NS{index + 1}:
            </span>
            <code
              className="text-sm text-blue-700 font-mono cursor-pointer hover:bg-blue-100 px-2 py-0.5 rounded"
              onClick={() => copy(ns, "ns")}
              title="Click to copy"
            >
              {ns}
            </code>
          </div>
        ))}
      </div>

      {/* Domains — full scrollable list, no truncation */}
      <div className="px-4 py-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-xs font-semibold text-gray-700">
            Domains ({filtered.length}
            {filtered.length !== group.domains.length ? ` of ${group.domains.length}` : ""})
          </span>
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
              onClick={() => copy(d, "domains")}
              className="px-3 py-1.5 text-sm font-mono text-gray-800 hover:bg-yellow-50 cursor-pointer flex items-center justify-between group"
              title="Click to copy this domain"
            >
              <span>{d}</span>
              <span className="text-[10px] text-gray-400 opacity-0 group-hover:opacity-100">
                click to copy
              </span>
            </div>
          ))}
          {filtered.length === 0 && (
            <div className="px-3 py-3 text-sm text-gray-400 text-center">No domains match filter</div>
          )}
        </div>
      </div>
    </div>
  );
};

export const NameserverGroupsDisplay = ({
  groups,
  totalDomains,
  onClose,
}: NameserverGroupsDisplayProps) => {
  const [allCopied, setAllCopied] = useState<string | null>(null);

  const allDomainsText = useMemo(
    () => groups.flatMap((g) => g.domains).join("\n"),
    [groups]
  );
  const allTableText = useMemo(
    () =>
      groups
        .flatMap((g) => g.domains.map((d) => [d, ...g.nameservers].join("\t")))
        .join("\n"),
    [groups]
  );

  const copyAll = async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setAllCopied(key);
      setTimeout(() => setAllCopied(null), 2000);
    } catch (err) {
      console.error("Failed to copy:", err);
    }
  };

  if (groups.length === 0) {
    return (
      <div className="bg-gray-50 border border-gray-200 rounded-lg p-6 text-center">
        <span className="text-4xl">📭</span>
        <p className="mt-2 text-gray-600">No nameserver groups to display</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Summary Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-lg font-semibold text-gray-900">
            Nameserver Groups
          </h3>
          <p className="text-sm text-gray-500 mt-1">
            {totalDomains} domain{totalDomains !== 1 ? "s" : ""} across {groups.length} nameserver group{groups.length !== 1 ? "s" : ""}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => copyAll(allDomainsText, "all-domains")}
            className={`px-3 py-1 text-xs font-medium rounded border transition-colors ${
              allCopied === "all-domains"
                ? "bg-green-100 text-green-700 border-green-300"
                : "bg-indigo-100 text-indigo-700 hover:bg-indigo-200 border-indigo-300"
            }`}
          >
            {allCopied === "all-domains" ? "✓ Copied!" : `📋 Copy ALL Domains (${totalDomains})`}
          </button>
          <button
            onClick={() => copyAll(allTableText, "all-table")}
            title="domain TAB ns1 TAB ns2 — one row per domain, all groups"
            className={`px-3 py-1 text-xs font-medium rounded border transition-colors ${
              allCopied === "all-table"
                ? "bg-green-100 text-green-700 border-green-300"
                : "bg-emerald-100 text-emerald-700 hover:bg-emerald-200 border-emerald-300"
            }`}
          >
            {allCopied === "all-table" ? "✓ Copied!" : "📋 Copy ALL as Table (TSV)"}
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="text-gray-400 hover:text-gray-600 transition-colors"
            >
              <span className="text-xl">×</span>
            </button>
          )}
        </div>
      </div>

      {/* Info Box */}
      <div className="bg-blue-50 border border-blue-200 rounded-lg p-4 text-sm text-blue-800">
        <div className="flex items-start gap-2">
          <span className="text-blue-500">💡</span>
          <div>
            <p className="font-medium">Next Step: Update Nameservers at Registrar</p>
            <p className="mt-1 text-blue-700">
              Copy the nameservers for each group and update them at your registrar (e.g., Porkbun). 
              You can bulk-update domains that share the same nameserver pair.
            </p>
          </div>
        </div>
      </div>

      {/* Nameserver Group Cards */}
      <div className="space-y-4">
        {groups.map((group, index) => (
          <NameserverGroupCard key={`${group.nameservers.join("-")}-${index}`} group={group} />
        ))}
      </div>
    </div>
  );
};

// Modal version of the display
interface NameserverGroupsModalProps {
  isOpen: boolean;
  onClose: () => void;
  groups: NameserverGroup[];
  totalDomains: number;
}

export const NameserverGroupsModal = ({
  isOpen,
  onClose,
  groups,
  totalDomains,
}: NameserverGroupsModalProps) => {
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl max-w-3xl w-full mx-4 max-h-[85vh] flex flex-col">
        {/* Header */}
        <div className="px-6 py-4 border-b border-gray-200 flex items-center justify-between shrink-0">
          <div>
            <h2 className="text-xl font-semibold text-gray-900">Zone Creation Complete</h2>
            <p className="text-sm text-gray-500 mt-1">
              Review nameserver groups and update at your registrar
            </p>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors"
          >
            <span className="text-2xl">×</span>
          </button>
        </div>

        {/* Scrollable Content */}
        <div className="flex-1 overflow-y-auto p-6">
          <NameserverGroupsDisplay 
            groups={groups} 
            totalDomains={totalDomains}
          />
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-gray-200 flex justify-end shrink-0">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700 transition-colors"
          >
            Done
          </button>
        </div>
      </div>
    </div>
  );
};

export default NameserverGroupsDisplay;