"use client";

import { useMemo, useState } from "react";
import {
  CloudflareZoneSetupResult,
  CloudflareZoneSetupResultItem,
  NameserverGroup,
  setupCloudflareZones,
} from "@/lib/api";
import { ToastContainer, useToasts } from "@/components/ui/Toast";

const DOMAIN_REGEX = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/i;

type ParsedDomains = {
  domains: string[];
  invalid: string[];
  duplicates: number;
};

const normalizeDomain = (value: string): string => {
  let domain = value.trim().toLowerCase().replace(/\.$/, "");
  domain = domain.replace(/^https?:\/\//, "");
  domain = domain.split("/", 1)[0];
  domain = domain.split("?", 1)[0];
  domain = domain.split("#", 1)[0];
  domain = domain.split("@").pop() || domain;
  domain = domain.split(":", 1)[0];
  return domain;
};

const parseDomains = (value: string): ParsedDomains => {
  const seen = new Set<string>();
  const domains: string[] = [];
  const invalid: string[] = [];
  let duplicates = 0;

  value
    .split(/[\n,;\s]+/)
    .map(normalizeDomain)
    .filter(Boolean)
    .forEach((domain) => {
      if (!DOMAIN_REGEX.test(domain)) {
        invalid.push(domain);
        return;
      }

      if (seen.has(domain)) {
        duplicates += 1;
        return;
      }

      seen.add(domain);
      domains.push(domain);
    });

  return { domains, invalid, duplicates };
};

const copyToClipboard = async (text: string) => {
  await navigator.clipboard.writeText(text);
};

const buildGroupedHandoff = (groups: NameserverGroup[]) =>
  groups
    .map(
      (group, index) =>
        [
          `Group ${index + 1}`,
          "Nameservers:",
          ...group.nameservers,
          "",
          `Domains (${group.domain_count}):`,
          ...group.domains,
        ].join("\n"),
    )
    .join("\n\n");

const buildTableText = (results: CloudflareZoneSetupResultItem[]) =>
  [
    ["Domain", "Nameserver 1", "Nameserver 2", "Zone Status"].join("\t"),
    ...results
      .filter((result) => result.success)
      .map((result) =>
        [
          result.domain,
          result.nameservers[0] || "",
          result.nameservers[1] || "",
          result.zone_status || "",
        ].join("\t"),
      ),
  ].join("\n");

const databaseActionLabel = (action: string | null) => {
  switch (action) {
    case "created":
      return "Saved";
    case "updated":
      return "Updated";
    case "updated_status_preserved":
      return "Updated, status kept";
    case "error_recorded":
      return "Error saved";
    default:
      return "Not changed";
  }
};

function CopyButton({
  label,
  copiedLabel = "Copied",
  text,
  disabled = false,
  onCopied,
}: {
  label: string;
  copiedLabel?: string;
  text: string;
  disabled?: boolean;
  onCopied: (label: string) => void;
}) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    if (!text || disabled) return;
    await copyToClipboard(text);
    setCopied(true);
    onCopied(label);
    setTimeout(() => setCopied(false), 1800);
  };

  return (
    <button
      type="button"
      onClick={handleCopy}
      disabled={disabled || !text}
      className={`inline-flex items-center justify-center rounded-md border px-3 py-2 text-sm font-medium transition-colors ${
        copied
          ? "border-green-300 bg-green-50 text-green-700"
          : "border-gray-300 bg-white text-gray-700 hover:bg-gray-50"
      } disabled:cursor-not-allowed disabled:opacity-50`}
    >
      {copied ? copiedLabel : label}
    </button>
  );
}

function NameserverGroupPanel({
  group,
  index,
  onCopied,
}: {
  group: NameserverGroup;
  index: number;
  onCopied: (label: string) => void;
}) {
  const domainsText = group.domains.join("\n");
  const nameserversText = group.nameservers.join("\n");
  const tableText = group.domains.map((domain) => [domain, ...group.nameservers].join("\t")).join("\n");

  return (
    <section className="rounded-lg border border-gray-200 bg-white">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-200 px-4 py-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <span className="rounded bg-gray-100 px-2 py-1 text-xs font-semibold text-gray-700">
              Group {index + 1}
            </span>
            <h3 className="text-base font-semibold text-gray-900">
              {group.domain_count} domain{group.domain_count !== 1 ? "s" : ""}
            </h3>
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {group.nameservers.map((nameserver) => (
              <code key={nameserver} className="rounded bg-blue-50 px-2 py-1 text-xs text-blue-700">
                {nameserver}
              </code>
            ))}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <CopyButton label="Copy NS" text={nameserversText} onCopied={onCopied} />
          <CopyButton label="Copy Domains" text={domainsText} onCopied={onCopied} />
          <CopyButton label="Copy TSV" text={tableText} onCopied={onCopied} />
        </div>
      </div>

      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wide text-gray-500">
            Nameservers
          </label>
          <textarea
            readOnly
            value={nameserversText}
            className="h-28 w-full resize-none rounded-md border border-gray-300 bg-gray-50 p-3 font-mono text-sm text-gray-900"
          />
        </div>
        <div>
          <label className="mb-2 block text-xs font-semibold uppercase tracking-wide text-gray-500">
            Domains
          </label>
          <textarea
            readOnly
            value={domainsText}
            className="h-28 w-full resize-y rounded-md border border-gray-300 bg-gray-50 p-3 font-mono text-sm text-gray-900"
          />
        </div>
      </div>
    </section>
  );
}

function ResultRow({
  result,
  onCopied,
}: {
  result: CloudflareZoneSetupResultItem;
  onCopied: (label: string) => void;
}) {
  const rowText = [result.domain, ...result.nameservers].join("\t");

  return (
    <tr className={result.success ? "bg-white" : "bg-red-50/60"}>
      <td className="whitespace-nowrap px-4 py-3 font-mono text-sm text-gray-900">{result.domain}</td>
      <td className="whitespace-nowrap px-4 py-3 text-sm">
        {result.success ? (
          <span className="rounded-full bg-green-100 px-2 py-1 text-xs font-medium text-green-700">
            Ready
          </span>
        ) : (
          <span className="rounded-full bg-red-100 px-2 py-1 text-xs font-medium text-red-700">
            Failed
          </span>
        )}
      </td>
      <td className="px-4 py-3 text-sm text-gray-700">
        {result.nameservers.length > 0 ? (
          <div className="space-y-1">
            {result.nameservers.map((nameserver) => (
              <code key={nameserver} className="block font-mono text-blue-700">
                {nameserver}
              </code>
            ))}
          </div>
        ) : (
          <span className="text-gray-400">None returned</span>
        )}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-600">
        {result.zone_status || "-"}
        {result.already_existed && <span className="ml-2 text-xs text-gray-400">existing</span>}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-sm text-gray-600">
        {databaseActionLabel(result.database_action)}
      </td>
      <td className="px-4 py-3 text-sm text-red-600">{result.error || ""}</td>
      <td className="whitespace-nowrap px-4 py-3 text-right text-sm">
        <CopyButton
          label="Copy Row"
          text={rowText}
          disabled={!result.success}
          onCopied={onCopied}
        />
      </td>
    </tr>
  );
}

export default function CloudflareZonesPage() {
  const [domainInput, setDomainInput] = useState("");
  const [result, setResult] = useState<CloudflareZoneSetupResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { toasts, dismissToast, success, error: toastError, warning } = useToasts();

  const parsed = useMemo(() => parseDomains(domainInput), [domainInput]);
  const successfulResults = useMemo(
    () => result?.results.filter((item) => item.success) || [],
    [result],
  );

  const allDomainsText = successfulResults.map((item) => item.domain).join("\n");
  const allTableText = buildTableText(successfulResults);
  const groupedHandoffText = result ? buildGroupedHandoff(result.nameserver_groups) : "";

  const handleCopied = (label: string) => {
    success(`${label} copied`);
  };

  const handleSubmit = async () => {
    if (parsed.domains.length === 0 || loading) return;

    setLoading(true);
    setError(null);
    setResult(null);

    if (parsed.invalid.length > 0) {
      warning("Some entries were skipped", `${parsed.invalid.length} invalid entr${parsed.invalid.length === 1 ? "y" : "ies"}`);
    }

    try {
      const response = await setupCloudflareZones(parsed.domains);
      setResult(response);
      if (response.failed > 0) {
        warning(
          `${response.success} zones ready, ${response.failed} failed`,
          "Review the per-domain results below.",
        );
      } else {
        success(`${response.success} Cloudflare zones ready`);
      }
    } catch (err) {
      console.error("Cloudflare zone setup failed:", err);
      const message = err instanceof Error ? err.message : "Cloudflare zone setup failed";
      setError(message);
      toastError("Cloudflare zone setup failed", message);
    } finally {
      setLoading(false);
    }
  };

  const overLimit = parsed.domains.length > 500;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Cloudflare Zone Setup</h1>
          <p className="mt-1 text-sm text-gray-500">
            Create zones and collect registrar nameservers without running the rest of setup.
          </p>
        </div>
      </div>

      {error && (
        <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_360px]">
        <section className="rounded-lg border border-gray-200 bg-white p-5">
          <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
            <label htmlFor="cloudflare-domains" className="block text-sm font-semibold text-gray-800">
              Domains
            </label>
            <div className="flex flex-wrap gap-2 text-xs text-gray-500">
              <span>{parsed.domains.length} valid</span>
              {parsed.duplicates > 0 && <span>{parsed.duplicates} duplicate ignored</span>}
              {parsed.invalid.length > 0 && <span className="text-amber-600">{parsed.invalid.length} invalid</span>}
            </div>
          </div>

          <textarea
            id="cloudflare-domains"
            value={domainInput}
            onChange={(event) => setDomainInput(event.target.value)}
            placeholder={"example.com\nsecondexample.co\nthirdexample.net"}
            className="min-h-72 w-full resize-y rounded-lg border border-gray-300 p-3 font-mono text-sm text-gray-900 outline-none transition focus:border-blue-500 focus:ring-2 focus:ring-blue-500"
          />

          {parsed.invalid.length > 0 && (
            <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
              Invalid entries: {parsed.invalid.slice(0, 6).join(", ")}
              {parsed.invalid.length > 6 ? ` and ${parsed.invalid.length - 6} more` : ""}
            </div>
          )}

          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={handleSubmit}
              disabled={loading || parsed.domains.length === 0 || overLimit}
              className="inline-flex min-w-44 items-center justify-center rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {loading ? (
                <span className="flex items-center gap-2">
                  <span className="h-4 w-4 animate-spin rounded-full border-2 border-white border-t-transparent" />
                  Setting up...
                </span>
              ) : (
                `Setup ${parsed.domains.length} Zone${parsed.domains.length !== 1 ? "s" : ""}`
              )}
            </button>

            <CopyButton
              label="Copy Parsed Domains"
              text={parsed.domains.join("\n")}
              disabled={parsed.domains.length === 0}
              onCopied={handleCopied}
            />

            <button
              type="button"
              onClick={() => {
                setDomainInput("");
                setResult(null);
                setError(null);
              }}
              className="rounded-md px-3 py-2 text-sm font-medium text-gray-600 hover:bg-gray-100"
            >
              Clear
            </button>
          </div>

          {overLimit && (
            <p className="mt-3 text-sm text-red-600">Maximum 500 domains per request.</p>
          )}
        </section>

        <aside className="rounded-lg border border-gray-200 bg-white p-5">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-gray-500">Copy Workspace</h2>
          <div className="mt-4 grid grid-cols-3 gap-3">
            <div>
              <div className="text-2xl font-bold text-gray-900">{result?.success || 0}</div>
              <div className="text-xs text-gray-500">Ready</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-red-600">{result?.failed || 0}</div>
              <div className="text-xs text-gray-500">Failed</div>
            </div>
            <div>
              <div className="text-2xl font-bold text-blue-600">{result?.nameserver_groups.length || 0}</div>
              <div className="text-xs text-gray-500">NS Groups</div>
            </div>
          </div>

          <div className="mt-5 space-y-2">
            <CopyButton
              label="Copy All Domains"
              text={allDomainsText}
              disabled={!successfulResults.length}
              onCopied={handleCopied}
            />
            <CopyButton
              label="Copy All as TSV"
              text={allTableText}
              disabled={!successfulResults.length}
              onCopied={handleCopied}
            />
            <CopyButton
              label="Copy Grouped Handoff"
              text={groupedHandoffText}
              disabled={!result?.nameserver_groups.length}
              onCopied={handleCopied}
            />
          </div>

          {result && (
            <div className="mt-5 rounded-md bg-gray-50 p-3 text-sm text-gray-600">
              {result.nameserver_groups.length === 1
                ? "All successful domains share the same nameservers."
                : `${result.nameserver_groups.length} different nameserver groups were returned. Use the grouped cards below for registrar bulk updates.`}
            </div>
          )}
        </aside>
      </div>

      {result && result.nameserver_groups.length > 0 && (
        <section className="space-y-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold text-gray-900">Grouped for Registrar Updates</h2>
              <p className="text-sm text-gray-500">
                Copy one group at a time when domains have different Cloudflare nameservers.
              </p>
            </div>
          </div>

          <div className="space-y-4">
            {result.nameserver_groups.map((group, index) => (
              <NameserverGroupPanel
                key={`${group.nameservers.join("-")}-${index}`}
                group={group}
                index={index}
                onCopied={handleCopied}
              />
            ))}
          </div>
        </section>
      )}

      {result && (
        <section className="rounded-lg border border-gray-200 bg-white">
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-200 px-4 py-3">
            <div>
              <h2 className="text-lg font-semibold text-gray-900">Per-Domain Results</h2>
              <p className="text-sm text-gray-500">
                {result.total} processed, {result.success} ready, {result.failed} failed
              </p>
            </div>
          </div>
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Domain
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Result
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Nameservers
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Zone
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Database
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Error
                  </th>
                  <th className="px-4 py-3 text-right text-xs font-semibold uppercase tracking-wide text-gray-500">
                    Copy
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200">
                {result.results.map((item) => (
                  <ResultRow key={item.domain} result={item} onCopied={handleCopied} />
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
