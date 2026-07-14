"use client";

import { useRouter } from "next/navigation";
import { Domain, DomainStatus } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";

interface DomainsTableProps {
  domains: Domain[];
  onConfirmNs?: (id: string) => void;
  onDelete?: (id: string) => void;
  selectedDomains?: Set<string>;
  onSelectDomain?: (id: string, selected: boolean) => void;
  onSelectAll?: (selected: boolean) => void;
}

const statusConfig: Record<DomainStatus, { label: string; variant: "success" | "warning" | "error" | "default" }> = {
  purchased: { label: "Purchased", variant: "default" },
  cf_zone_pending: { label: "CF Zone Pending", variant: "warning" },
  cf_zone_active: { label: "CF Zone Active", variant: "success" },
  zone_created: { label: "Zone Created", variant: "success" },
  ns_updating: { label: "NS Updating", variant: "warning" },
  ns_propagating: { label: "NS Propagating", variant: "warning" },
  ns_propagated: { label: "NS Propagated", variant: "success" },
  dns_configuring: { label: "DNS Configuring", variant: "warning" },
  tenant_linked: { label: "Tenant Linked", variant: "warning" },
  pending_m365: { label: "Pending M365", variant: "warning" },
  m365_verified: { label: "M365 Verified", variant: "success" },
  pending_dkim: { label: "Pending DKIM", variant: "warning" },
  active: { label: "Active", variant: "success" },
  problem: { label: "Problem", variant: "error" },
  error: { label: "Error", variant: "error" },
  retired: { label: "Retired", variant: "default" },
};

const formatDate = (dateString: string): string => {
  return new Date(dateString).toLocaleDateString("en-NZ", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
};

export function DomainsTable({ 
  domains, 
  onConfirmNs, 
  onDelete,
  selectedDomains = new Set(),
  onSelectDomain,
  onSelectAll,
}: DomainsTableProps) {
  const router = useRouter();
  const allSelected = domains.length > 0 && domains.every((d) => selectedDomains.has(d.id));
  const someSelected = domains.some((d) => selectedDomains.has(d.id));

  const handleRowClick = (id: string) => {
    router.push(`/domains/${id}`);
  };

  if (domains.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
        <div className="text-4xl mb-4">🌐</div>
        <h3 className="text-lg font-medium text-gray-900 mb-2">No domains yet</h3>
        <p className="text-gray-500 mb-4">Add your first domain to get started with email infrastructure.</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
      <table className="min-w-full divide-y divide-gray-200">
        <thead className="bg-gray-50">
          <tr>
            {onSelectAll && (
              <th className="px-4 py-3 text-left">
                <input
                  type="checkbox"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someSelected && !allSelected;
                  }}
                  onChange={(e) => onSelectAll(e.target.checked)}
                  className="h-4 w-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500"
                />
              </th>
            )}
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Domain
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Status
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Nameservers
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              DNS Records
            </th>
            <th className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">
              Created
            </th>
            <th className="px-6 py-3 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">
              Actions
            </th>
          </tr>
        </thead>
        <tbody className="bg-white divide-y divide-gray-200">
          {domains.map((domain) => {
            const statusInfo = statusConfig[domain.status] || { label: domain.status, variant: "default" as const };
            const dnsChecks = [
              { label: "MX", verified: domain.mx_configured },
              { label: "SPF", verified: domain.spf_configured },
              { label: "DKIM", verified: domain.dkim_enabled },
              { label: "DMARC", verified: domain.dmarc_configured },
            ];

            return (
              <tr
                key={domain.id}
                onClick={() => handleRowClick(domain.id)}
                className={`hover:bg-gray-50 cursor-pointer transition-colors ${
                  selectedDomains.has(domain.id) ? "bg-blue-50" : ""
                }`}
              >
                {onSelectDomain && (
                  <td className="px-4 py-4 whitespace-nowrap" onClick={(e) => e.stopPropagation()}>
                    <input
                      type="checkbox"
                      checked={selectedDomains.has(domain.id)}
                      onChange={(e) => onSelectDomain(domain.id, e.target.checked)}
                      className="h-4 w-4 text-blue-600 rounded border-gray-300 focus:ring-blue-500"
                    />
                  </td>
                )}
                <td className="px-6 py-4 whitespace-nowrap">
                  <div className="flex items-center">
                    <span className="text-lg mr-3">🌐</span>
                    <div>
                      <div className="text-sm font-medium text-gray-900">{domain.name}</div>
                      <div className="text-xs text-gray-500 font-mono">
                        {domain.id.slice(0, 8)}...
                      </div>
                    </div>
                  </div>
                </td>
                <td className="px-6 py-4 whitespace-nowrap">
                  <Badge variant={statusInfo.variant}>{statusInfo.label}</Badge>
                </td>
                <td className="px-6 py-4 whitespace-nowrap">
                  {domain.cloudflare_nameservers && domain.cloudflare_nameservers.length > 0 ? (
                    <div className="text-xs font-mono text-gray-600">
                      {domain.cloudflare_nameservers.slice(0, 2).map((ns, i) => (
                        <div key={i}>{ns}</div>
                      ))}
                    </div>
                  ) : (
                    <span className="text-gray-400 text-sm">—</span>
                  )}
                </td>
                <td className="px-6 py-4 whitespace-nowrap">
                  <div className="flex gap-1">
                    {dnsChecks.map((check) => (
                      <span
                        key={check.label}
                        className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                          check.verified
                            ? "bg-green-100 text-green-800"
                            : "bg-gray-100 text-gray-500"
                        }`}
                        title={`${check.label}: ${check.verified ? "Verified" : "Not verified"}`}
                      >
                        {check.label}
                      </span>
                    ))}
                  </div>
                </td>
                <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-500">
                  {formatDate(domain.created_at)}
                </td>
                <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                  <div className="flex justify-end gap-2" onClick={(e) => e.stopPropagation()}>
                    {domain.status === "ns_updating" && onConfirmNs && (
                      <button
                        onClick={() => onConfirmNs(domain.id)}
                        className="text-blue-600 hover:text-blue-900 text-xs px-2 py-1 rounded border border-blue-200 hover:bg-blue-50"
                      >
                        Confirm NS
                      </button>
                    )}
                    {onDelete && domain.status !== "retired" && (
                      <button
                        onClick={() => onDelete(domain.id)}
                        className="text-red-600 hover:text-red-900 text-xs px-2 py-1 rounded border border-red-200 hover:bg-red-50"
                        title="Retire domain"
                      >
                        🗑️
                      </button>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
