"use client";

import { useState, useEffect, useCallback } from "react";
import Link from "next/link";
import { 
  Domain, 
  DomainStatus,
  listDomains, 
  confirmNameservers, 
  bulkDeleteDomains, 
  deleteDomain,
  bulkImportDomains,
  bulkCreateZones,
  checkPropagation,
  bulkSetupRedirects,
  BulkImportResult,
  BulkZoneResult,
  PropagationResult,
  BulkRedirectResult,
  NameserverGroup,
} from "@/lib/api";
import { DomainsTable } from "@/components/domains/DomainsTable";
import { BulkImportModal } from "@/components/domains/BulkImportModal";
import { ImportResultsModal } from "@/components/domains/ImportResultsModal";
import { NameserverGroupsModal } from "@/components/domains/NameserverGroupsDisplay";
import { ToastContainer, useToasts } from "@/components/ui/Toast";

// Status filter options
const STATUS_OPTIONS: { value: DomainStatus | "all"; label: string }[] = [
  { value: "all", label: "All Statuses" },
  { value: "purchased", label: "Purchased" },
  { value: "cf_zone_pending", label: "Zone Pending" },
  { value: "cf_zone_active", label: "Zone Active" },
  { value: "zone_created", label: "Zone Created" },
  { value: "ns_updating", label: "NS Updating" },
  { value: "ns_propagating", label: "NS Propagating" },
  { value: "ns_propagated", label: "NS Propagated" },
  { value: "dns_configuring", label: "DNS Configuring" },
  { value: "tenant_linked", label: "Tenant Linked" },
  { value: "pending_m365", label: "Pending M365" },
  { value: "m365_verified", label: "M365 Verified" },
  { value: "pending_dkim", label: "Pending DKIM" },
  { value: "active", label: "Active" },
  { value: "problem", label: "Problem" },
  { value: "error", label: "Error" },
  { value: "retired", label: "Retired" },
];

export default function DomainsPage() {
  // Core state
  const [domains, setDomains] = useState<Domain[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [selectedDomains, setSelectedDomains] = useState<Set<string>>(new Set());
  
  // Filter state
  const [statusFilter, setStatusFilter] = useState<DomainStatus | "all">("all");
  
  // Modal state
  const [showImportModal, setShowImportModal] = useState(false);
  const [showResultsModal, setShowResultsModal] = useState(false);
  const [showNsGroupsModal, setShowNsGroupsModal] = useState(false);
  
  // Results state
  const [importResults, setImportResults] = useState<BulkImportResult | null>(null);
  const [nsGroups, setNsGroups] = useState<NameserverGroup[]>([]);
  const [nsTotalDomains, setNsTotalDomains] = useState(0);
  const [propagationResults, setPropagationResults] = useState<PropagationResult | null>(null);
  const [redirectResults, setRedirectResults] = useState<BulkRedirectResult | null>(null);
  
  // Bulk action loading state
  const [bulkActionName, setBulkActionName] = useState<string | null>(null);
  
  // Toast notifications
  const { toasts, dismissToast, success, error: toastError, info } = useToasts();

  const fetchDomains = useCallback(async () => {
    try {
      setError(null);
      const data = await listDomains();
      setDomains(data);
    } catch (err) {
      console.error("Failed to fetch domains:", err);
      setError("Failed to load domains. Make sure the backend is running.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDomains();
  }, [fetchDomains]);

  const handleConfirmNs = async (id: string) => {
    setActionLoading(id);
    try {
      const updated = await confirmNameservers(id);
      setDomains((prev) => prev.map((d) => (d.id === id ? updated : d)));
    } catch (err) {
      console.error("Failed to confirm nameservers:", err);
      alert("Failed to confirm nameservers. Please try again.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleDeleteDomain = async (id: string) => {
    if (!confirm("Are you sure you want to retire this domain?")) return;
    setActionLoading(id);
    try {
      await deleteDomain(id);
      setDomains((prev) => prev.filter((d) => d.id !== id));
      setSelectedDomains((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    } catch (err) {
      console.error("Failed to delete domain:", err);
      alert("Failed to delete domain. Please try again.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleBulkDelete = async () => {
    if (selectedDomains.size === 0) return;
    if (!confirm(`Are you sure you want to retire ${selectedDomains.size} domain(s)?`)) return;
    setActionLoading("bulk");
    try {
      await bulkDeleteDomains(Array.from(selectedDomains));
      setDomains((prev) => prev.filter((d) => !selectedDomains.has(d.id)));
      setSelectedDomains(new Set());
    } catch (err) {
      console.error("Failed to bulk delete domains:", err);
      alert("Failed to delete domains. Please try again.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleSelectDomain = (id: string, selected: boolean) => {
    setSelectedDomains((prev) => {
      const next = new Set(prev);
      if (selected) {
        next.add(id);
      } else {
        next.delete(id);
      }
      return next;
    });
  };

  const handleSelectAll = (selected: boolean) => {
    if (selected) {
      setSelectedDomains(new Set(filteredDomains.map((d) => d.id)));
    } else {
      setSelectedDomains(new Set());
    }
  };

  // Bulk Import handler
  const handleBulkImport = async (file: File) => {
    setBulkActionName("Importing domains...");
    try {
      const results = await bulkImportDomains(file);
      setImportResults(results);
      setShowImportModal(false);
      setShowResultsModal(true);
      await fetchDomains();
      if (results.created > 0) {
        success(`Imported ${results.created} domains`, `${results.skipped} skipped, ${results.failed} failed`);
      } else {
        info("No new domains imported", `${results.skipped} skipped, ${results.failed} failed`);
      }
    } catch (err) {
      console.error("Failed to import domains:", err);
      toastError("Import failed", "Please check the CSV format and try again.");
    } finally {
      setBulkActionName(null);
    }
  };

  // Bulk Create Zones handler
  const handleBulkCreateZones = async () => {
    const purchasedCount = selectedDomains.size > 0 
      ? filteredDomains.filter(d => selectedDomains.has(d.id) && d.status === "purchased").length
      : filteredDomains.filter(d => d.status === "purchased").length;
    
    if (purchasedCount === 0) {
      toastError("No domains to process", "Select domains with 'purchased' status or filter by purchased status.");
      return;
    }
    
    setBulkActionName(`Creating zones for ${purchasedCount} domains...`);
    try {
      const domainIds = selectedDomains.size > 0 
        ? Array.from(selectedDomains).filter(id => {
            const d = domains.find(dom => dom.id === id);
            return d && d.status === "purchased";
          })
        : undefined;
      
      const result = await bulkCreateZones(domainIds);
      setNsGroups(result.nameserver_groups);
      setNsTotalDomains(result.success);
      setShowNsGroupsModal(true);
      await fetchDomains();
      setSelectedDomains(new Set());
      success(`Created ${result.success} zones`, `${result.failed} failed`);
    } catch (err) {
      console.error("Failed to create zones:", err);
      toastError("Zone creation failed", "Please try again.");
    } finally {
      setBulkActionName(null);
    }
  };

  // Check Propagation handler
  const handleCheckPropagation = async () => {
    setBulkActionName("Checking NS propagation...");
    try {
      const domainIds = selectedDomains.size > 0 ? Array.from(selectedDomains) : undefined;
      const result = await checkPropagation(domainIds);
      setPropagationResults(result);
      await fetchDomains();
      if (result.propagated > 0) {
        success(`${result.propagated} domains propagated`, `${result.pending} still pending`);
      } else {
        info("No domains have propagated yet", `${result.pending} still pending`);
      }
    } catch (err) {
      console.error("Failed to check propagation:", err);
      toastError("Propagation check failed", "Please try again.");
    } finally {
      setBulkActionName(null);
    }
  };

  // Setup Redirects handler
  const handleSetupRedirects = async () => {
    setBulkActionName("Setting up redirects...");
    try {
      const domainIds = selectedDomains.size > 0 ? Array.from(selectedDomains) : undefined;
      const result = await bulkSetupRedirects(domainIds);
      setRedirectResults(result);
      await fetchDomains();
      if (result.success > 0) {
        success(`Setup ${result.success} redirects`, `${result.failed} failed`);
      } else {
        info("No redirects configured", "No domains with redirect_url found or already configured.");
      }
    } catch (err) {
      console.error("Failed to setup redirects:", err);
      toastError("Redirect setup failed", "Please try again.");
    } finally {
      setBulkActionName(null);
    }
  };

  // Filter domains by status
  const filteredDomains = statusFilter === "all" 
    ? domains 
    : domains.filter(d => d.status === statusFilter);

  // Get counts for bulk action buttons
  const purchasedCount = filteredDomains.filter(d => d.status === "purchased").length;
  const selectedPurchasedCount = selectedDomains.size > 0
    ? filteredDomains.filter(d => selectedDomains.has(d.id) && d.status === "purchased").length
    : purchasedCount;

  if (loading) {
    return (
      <div className="space-y-6">
        <div className="flex justify-between items-center">
          <div className="h-8 w-48 bg-gray-200 rounded animate-pulse" />
          <div className="h-10 w-32 bg-gray-200 rounded animate-pulse" />
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-12 text-center">
          <div className="text-2xl animate-spin inline-block">⏳</div>
          <p className="mt-4 text-gray-500">Loading domains...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Page Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Domains</h1>
          <p className="text-gray-500 mt-1">
            Manage your email domains and DNS configuration
          </p>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowImportModal(true)}
            className="inline-flex items-center px-4 py-2 bg-green-600 text-white text-sm font-medium rounded-lg hover:bg-green-700 transition-colors"
          >
            <span className="mr-2">📥</span>
            Import CSV
          </button>
          <Link
            href="/domains/new"
            className="inline-flex items-center px-4 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors"
          >
            <span className="mr-2">+</span>
            Add Domain
          </Link>
        </div>
      </div>

      {/* Filter and Bulk Actions Row */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Status Filter */}
        <div className="flex items-center gap-2">
          <label className="text-sm font-medium text-gray-700">Filter:</label>
          <select
            value={statusFilter}
            onChange={(e) => {
              setStatusFilter(e.target.value as DomainStatus | "all");
              setSelectedDomains(new Set());
            }}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
          >
            {STATUS_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
          {statusFilter !== "all" && (
            <span className="text-sm text-gray-500">
              ({filteredDomains.length} shown)
            </span>
          )}
        </div>

        {/* Bulk Action Buttons */}
        <div className="flex-1" />
        
        {selectedDomains.size > 0 && (
          <button
            onClick={handleBulkDelete}
            disabled={!!bulkActionName}
            className="inline-flex items-center px-3 py-2 bg-red-600 text-white text-sm font-medium rounded-lg hover:bg-red-700 transition-colors disabled:opacity-50"
          >
            <span className="mr-1">🗑️</span>
            Delete ({selectedDomains.size})
          </button>
        )}

        <button
          onClick={handleBulkCreateZones}
          disabled={!!bulkActionName || selectedPurchasedCount === 0}
          className="inline-flex items-center px-3 py-2 bg-purple-600 text-white text-sm font-medium rounded-lg hover:bg-purple-700 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span className="mr-1">🌐</span>
          Create Zones ({selectedPurchasedCount})
        </button>

        <button
          onClick={handleCheckPropagation}
          disabled={!!bulkActionName}
          className="inline-flex items-center px-3 py-2 bg-blue-600 text-white text-sm font-medium rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-50"
        >
          <span className="mr-1">🔍</span>
          Check Propagation
        </button>

        <button
          onClick={handleSetupRedirects}
          disabled={!!bulkActionName}
          className="inline-flex items-center px-3 py-2 bg-orange-600 text-white text-sm font-medium rounded-lg hover:bg-orange-700 transition-colors disabled:opacity-50"
        >
          <span className="mr-1">🔗</span>
          Setup Redirects
        </button>
      </div>

      {/* Stats Row */}
      <div className="grid grid-cols-4 gap-4">
        <div className="bg-white rounded-lg border border-gray-200 px-4 py-3">
          <div className="text-2xl font-bold text-gray-900">{domains.length}</div>
          <div className="text-sm text-gray-500">Total Domains</div>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 px-4 py-3">
          <div className="text-2xl font-bold text-green-600">
            {domains.filter((d) => d.status === "active").length}
          </div>
          <div className="text-sm text-gray-500">Active</div>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 px-4 py-3">
          <div className="text-2xl font-bold text-yellow-600">
            {domains.filter((d) => d.status === "ns_updating" || d.status === "ns_propagating" || d.status === "cf_zone_pending").length}
          </div>
          <div className="text-sm text-gray-500">Pending</div>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 px-4 py-3">
          <div className="text-2xl font-bold text-blue-600">
            {domains.filter((d) => d.mx_configured && d.spf_configured && d.dkim_enabled && d.dmarc_configured).length}
          </div>
          <div className="text-sm text-gray-500">Fully Configured</div>
        </div>
      </div>

      {/* Error State */}
      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg p-4 flex items-start">
          <span className="text-red-500 mr-3">⚠️</span>
          <div>
            <h4 className="text-red-800 font-medium">Error loading domains</h4>
            <p className="text-red-600 text-sm mt-1">{error}</p>
            <button
              onClick={() => {
                setLoading(true);
                fetchDomains();
              }}
              className="mt-2 text-sm text-red-700 underline hover:no-underline"
            >
              Try again
            </button>
          </div>
        </div>
      )}

      {/* Action Loading Overlay */}
      {(actionLoading || bulkActionName) && (
        <div className="fixed inset-0 bg-black/20 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 shadow-xl text-center">
            <div className="text-2xl animate-spin inline-block mb-2">⏳</div>
            <p className="text-gray-600">{bulkActionName || "Processing..."}</p>
          </div>
        </div>
      )}

      {/* Domains Table */}
      {!error && (
        <DomainsTable
          domains={filteredDomains}
          onConfirmNs={handleConfirmNs}
          onDelete={handleDeleteDomain}
          selectedDomains={selectedDomains}
          onSelectDomain={handleSelectDomain}
          onSelectAll={handleSelectAll}
        />
      )}

      {/* Modals */}
      <BulkImportModal
        isOpen={showImportModal}
        onClose={() => setShowImportModal(false)}
        onImport={handleBulkImport}
        isLoading={!!bulkActionName}
      />

      <ImportResultsModal
        isOpen={showResultsModal}
        onClose={() => setShowResultsModal(false)}
        results={importResults}
      />

      <NameserverGroupsModal
        isOpen={showNsGroupsModal}
        onClose={() => setShowNsGroupsModal(false)}
        groups={nsGroups}
        totalDomains={nsTotalDomains}
      />

      {/* Toast Notifications */}
      <ToastContainer toasts={toasts} onDismiss={dismissToast} />
    </div>
  );
}
