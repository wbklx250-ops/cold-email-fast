"use client";

import { useMemo, useState } from "react";
import { exportMailboxCredentialsByDomains, HttpError, type DomainMailboxExportResult } from "@/lib/api";

const parseDomains = (value: string): string[] => {
  const seen = new Set<string>();
  const domains: string[] = [];

  value
    .split(/[\s,;]+/)
    .map((item) => item.trim())
    .filter(Boolean)
    .forEach((item) => {
      const normalized = item.toLowerCase();
      if (!seen.has(normalized)) {
        seen.add(normalized);
        domains.push(item);
      }
    });

  return domains;
};

const errorMessageFromDetails = (details: unknown): string | null => {
  if (!details || typeof details !== "object") {
    return null;
  }

  const detail = (details as { detail?: unknown }).detail;
  if (typeof detail === "string") {
    return detail;
  }

  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    const invalidDomains = (detail as { invalid_domains?: unknown }).invalid_domains;
    const domains = (detail as { domains?: unknown }).domains;

    if (Array.isArray(invalidDomains) && invalidDomains.length > 0) {
      return `${typeof message === "string" ? message : "Invalid domain values"}: ${invalidDomains.join(", ")}`;
    }

    if (Array.isArray(domains) && domains.length > 0) {
      return `${typeof message === "string" ? message : "No mailboxes found"}: ${domains.join(", ")}`;
    }

    if (typeof message === "string") {
      return message;
    }
  }

  return null;
};

export default function DomainMailboxExport() {
  const [domainInput, setDomainInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DomainMailboxExportResult | null>(null);

  const domains = useMemo(() => parseDomains(domainInput), [domainInput]);

  const handleExport = async () => {
    if (domains.length === 0) {
      setError("Enter at least one domain.");
      setResult(null);
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const exportResult = await exportMailboxCredentialsByDomains(domains);
      setResult(exportResult);
    } catch (err) {
      if (err instanceof HttpError) {
        setError(errorMessageFromDetails(err.details) || "Failed to export mailboxes for those domains.");
      } else {
        setError("Failed to export mailboxes for those domains.");
      }
    } finally {
      setLoading(false);
    }
  };

  return (
    <section className="bg-white rounded-lg border border-gray-200 p-6">
      <div className="mb-4">
        <h2 className="text-xl font-semibold text-gray-900">Export Mailboxes by Domain</h2>
        <p className="mt-1 text-sm text-gray-500">Download an upload-ready CSV for selected mailbox domains.</p>
      </div>

      <div className="space-y-3">
        <label className="block text-sm font-medium text-gray-700" htmlFor="mailbox-domain-export">
          Domains
        </label>
        <textarea
          id="mailbox-domain-export"
          value={domainInput}
          onChange={(event) => setDomainInput(event.target.value)}
          rows={6}
          className="w-full resize-y rounded-lg border border-gray-300 px-3 py-2 font-mono text-sm text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
          placeholder={"example.com\nanotherdomain.com\nthird-domain.co"}
          disabled={loading}
        />

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={handleExport}
            disabled={loading || domains.length === 0}
            className={`px-5 py-2.5 rounded-lg font-medium text-white transition-colors ${
              loading || domains.length === 0
                ? "bg-gray-400 cursor-not-allowed"
                : "bg-green-600 hover:bg-green-700"
            }`}
          >
            {loading ? "Exporting..." : "Download CSV"}
          </button>
          <span className="text-sm text-gray-500">
            {domains.length} domain{domains.length === 1 ? "" : "s"} selected
          </span>
        </div>
      </div>

      {result && (
        <div className="mt-4 rounded-lg border border-green-200 bg-green-50 p-4 text-sm text-green-700">
          Downloaded {result.mailboxCount} mailbox{result.mailboxCount === 1 ? "" : "es"} from {result.domainCount} domain{result.domainCount === 1 ? "" : "s"}.
          {result.missingDomains.length > 0 && (
            <span className="block mt-1 text-amber-700">
              No matching mailboxes: {result.missingDomains.join(", ")}
            </span>
          )}
        </div>
      )}

      {error && (
        <div className="mt-4 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}
    </section>
  );
}
