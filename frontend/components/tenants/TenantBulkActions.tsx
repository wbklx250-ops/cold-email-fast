"use client";

import { useState } from "react";
import {
  Tenant,
  TenantStatus,
  TenantBulkOperationResult,
  bulkCreateMailboxesInM365,
  bulkConfigureMailboxes,
} from "@/lib/api";

type BulkOperation = 
  | "create_mailboxes" 
  | "configure_mailboxes";

interface TenantBulkActionsProps {
  selectedTenants: Tenant[];
  onOperationStart: (operation: string) => void;
  onOperationComplete: (operation: string, result: TenantBulkOperationResult) => void;
  onOperationError: (operation: string, error: string) => void;
  onOpenGenerateMailboxes: () => void;
}

interface OperationConfig {
  label: string;
  icon: string;
  description: string;
  validStatuses: TenantStatus[];
  action: (ids?: string[]) => Promise<TenantBulkOperationResult>;
  color: string;
}

const OPERATIONS: Record<BulkOperation, OperationConfig> = {
  create_mailboxes: {
    label: "Create in M365",
    icon: "[+]",
    description: "Create mailbox accounts in M365",
    validStatuses: ["dkim_enabled", "mailboxes_creating"],
    action: bulkCreateMailboxesInM365,
    color: "green",
  },
  configure_mailboxes: {
    label: "Configure Mailboxes",
    icon: "[CFG]",
    description: "Set passwords and delegate access",
    validStatuses: ["mailboxes_creating", "mailboxes_configuring"],
    action: bulkConfigureMailboxes,
    color: "amber",
  },
};

export const TenantBulkActions = ({
  selectedTenants,
  onOperationStart,
  onOperationComplete,
  onOperationError,
  onOpenGenerateMailboxes,
}: TenantBulkActionsProps) => {
  const [activeOperation, setActiveOperation] = useState<BulkOperation | null>(null);
  const [progress, setProgress] = useState<{
    current: number;
    total: number;
    status: string;
  } | null>(null);

  const getEligibleTenants = (operation: BulkOperation): Tenant[] => {
    const config = OPERATIONS[operation];
    return selectedTenants.filter((t) => config.validStatuses.includes(t.status));
  };

  const handleOperation = async (operation: BulkOperation) => {
    const config = OPERATIONS[operation];
    const eligibleTenants = getEligibleTenants(operation);
    
    if (eligibleTenants.length === 0) {
      onOperationError(
        config.label,
        `No selected tenants are eligible for this operation. Required status: ${config.validStatuses.join(", ")}`
      );
      return;
    }

    setActiveOperation(operation);
    setProgress({ current: 0, total: eligibleTenants.length, status: "Starting..." });
    onOperationStart(config.label);

    try {
      const tenantIds = eligibleTenants.map((t) => t.id);
      
      // Simulate progress updates (actual API call is atomic)
      const progressInterval = setInterval(() => {
        setProgress((prev) => {
          if (!prev) return null;
          const newCurrent = Math.min(prev.current + 1, prev.total - 1);
          return {
            ...prev,
            current: newCurrent,
            status: `Processing ${newCurrent + 1} of ${prev.total}...`,
          };
        });
      }, 1500);

      const result = await config.action(tenantIds);
      
      clearInterval(progressInterval);
      setProgress({ 
        current: result.processed, 
        total: result.total, 
        status: `Completed: ${result.succeeded} succeeded, ${result.failed} failed` 
      });
      
      onOperationComplete(config.label, result);
      
      // Keep progress visible briefly
      setTimeout(() => {
        setActiveOperation(null);
        setProgress(null);
      }, 3000);
    } catch (err: unknown) {
      console.error(`Failed ${operation}:`, err);
      const errorMessage = err instanceof Error ? err.message : "Operation failed";
      onOperationError(config.label, errorMessage);
      setActiveOperation(null);
      setProgress(null);
    }
  };

  const colorClasses: Record<string, string> = {
    blue: "bg-blue-600 hover:bg-blue-700 text-white",
    indigo: "bg-indigo-600 hover:bg-indigo-700 text-white",
    purple: "bg-purple-600 hover:bg-purple-700 text-white",
    green: "bg-green-600 hover:bg-green-700 text-white",
    amber: "bg-amber-600 hover:bg-amber-700 text-white",
  };

  const disabledClasses = "bg-gray-200 text-gray-400 cursor-not-allowed";

  return (
    <div className="space-y-4">
      {/* Bulk Action Buttons */}
      <div className="flex flex-wrap gap-2">
        {(Object.entries(OPERATIONS) as [BulkOperation, OperationConfig][]).map(
          ([key, config]) => {
            const eligibleCount = getEligibleTenants(key).length;
            const isDisabled = eligibleCount === 0 || activeOperation !== null;
            const isActive = activeOperation === key;

            return (
              <button
                key={key}
                onClick={() => handleOperation(key)}
                disabled={isDisabled}
                title={`${config.description}${eligibleCount > 0 ? ` (${eligibleCount} eligible)` : " (no eligible tenants)"}`}
                className={`
                  px-3 py-2 text-sm font-medium rounded-lg transition-all flex items-center gap-2
                  ${isActive ? "ring-2 ring-offset-2 ring-gray-400" : ""}
                  ${isDisabled ? disabledClasses : colorClasses[config.color]}
                `}
              >
                {isActive ? (
                  <span className="animate-spin">*</span>
                ) : (
                  <span>{config.icon}</span>
                )}
                {config.label}
                {eligibleCount > 0 && !isActive && (
                  <span className="bg-white/20 px-1.5 py-0.5 rounded text-xs">
                    {eligibleCount}
                  </span>
                )}
              </button>
            );
          }
        )}

        {/* Generate Mailboxes Button (opens modal) */}
        <button
          onClick={onOpenGenerateMailboxes}
          disabled={activeOperation !== null}
          className={`
            px-3 py-2 text-sm font-medium rounded-lg transition-all flex items-center gap-2
            ${activeOperation !== null ? disabledClasses : "bg-pink-600 hover:bg-pink-700 text-white"}
          `}
          title="Generate mailbox records in database"
        >
          <span>[GEN]</span>
          Generate Mailboxes
        </button>
      </div>

      {/* Progress Indicator */}
      {progress && activeOperation && (
        <div className="bg-gray-50 border border-gray-200 rounded-lg p-4">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-medium text-gray-700">
              {OPERATIONS[activeOperation].label}
            </span>
            <span className="text-sm text-gray-500">
              {progress.current}/{progress.total}
            </span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2 mb-2">
            <div
              className="bg-purple-600 h-2 rounded-full transition-all duration-300"
              style={{
                width: `${(progress.current / progress.total) * 100}%`,
              }}
            />
          </div>
          <p className="text-xs text-gray-500">{progress.status}</p>
        </div>
      )}

      {/* Selection Summary */}
      {selectedTenants.length > 0 && (
        <div className="bg-purple-50 border border-purple-200 rounded-lg p-3">
          <p className="text-sm text-purple-700">
            <span className="font-medium">{selectedTenants.length}</span> tenant(s) selected
          </p>
          <div className="mt-2 flex flex-wrap gap-2 text-xs">
            {Object.entries(
              selectedTenants.reduce((acc, t) => {
                acc[t.status] = (acc[t.status] || 0) + 1;
                return acc;
              }, {} as Record<string, number>)
            ).map(([status, count]) => (
              <span
                key={status}
                className="bg-purple-100 text-purple-700 px-2 py-1 rounded"
              >
                {status}: {count}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* No Selection Message */}
      {selectedTenants.length === 0 && (
        <div className="text-sm text-gray-500 italic">
          Select tenants using the checkboxes to enable bulk actions
        </div>
      )}
    </div>
  );
};

export default TenantBulkActions;
