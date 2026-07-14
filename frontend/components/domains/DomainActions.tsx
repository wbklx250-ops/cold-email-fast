"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { Domain, confirmNameservers, verifyDnsRecords, deleteDomain } from "@/lib/api";

interface DomainActionsProps {
  domain: Domain;
  onUpdate: (domain: Domain) => void;
}

type ActionState = "idle" | "loading" | "success" | "error";

interface ActionFeedback {
  state: ActionState;
  message?: string;
}

export function DomainActions({ domain, onUpdate }: DomainActionsProps) {
  const router = useRouter();
  const [confirmNsState, setConfirmNsState] = useState<ActionFeedback>({ state: "idle" });
  const [verifyDnsState, setVerifyDnsState] = useState<ActionFeedback>({ state: "idle" });
  const [deleteState, setDeleteState] = useState<ActionFeedback>({ state: "idle" });
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);

  const handleConfirmNs = async () => {
    setConfirmNsState({ state: "loading" });
    try {
      const updated = await confirmNameservers(domain.id);
      setConfirmNsState({ state: "success", message: "Nameservers confirmed!" });
      onUpdate(updated);
      setTimeout(() => setConfirmNsState({ state: "idle" }), 3000);
    } catch (err) {
      console.error("Failed to confirm nameservers:", err);
      setConfirmNsState({ state: "error", message: "Failed to confirm. NS may not be propagated yet." });
    }
  };

  const handleVerifyDns = async () => {
    setVerifyDnsState({ state: "loading" });
    try {
      const updated = await verifyDnsRecords(domain.id);
      const allVerified = updated.mx_configured && updated.spf_configured && updated.dkim_enabled && updated.dmarc_configured;
      setVerifyDnsState({ 
        state: allVerified ? "success" : "idle", 
        message: allVerified ? "All DNS records verified!" : "Some records still propagating..." 
      });
      onUpdate(updated);
      if (!allVerified) {
        setTimeout(() => setVerifyDnsState({ state: "idle" }), 3000);
      }
    } catch (err) {
      console.error("Failed to verify DNS:", err);
      setVerifyDnsState({ state: "error", message: "Failed to verify DNS records" });
    }
  };

  const handleDelete = async () => {
    setDeleteState({ state: "loading" });
    try {
      await deleteDomain(domain.id);
      setDeleteState({ state: "success", message: "Domain deleted" });
      setTimeout(() => router.push("/domains"), 1000);
    } catch (err) {
      console.error("Failed to delete domain:", err);
      setDeleteState({ state: "error", message: "Failed to delete domain" });
      setShowDeleteConfirm(false);
    }
  };

  const getButtonStyles = (state: ActionState, variant: "primary" | "secondary" | "danger" = "primary") => {
    const base = "px-4 py-3 text-sm font-medium rounded-lg transition-all flex items-center justify-center gap-2";
    
    if (state === "loading") return `${base} bg-gray-100 text-gray-500 cursor-wait`;
    if (state === "success") return `${base} bg-green-100 text-green-700`;
    if (state === "error") return `${base} bg-red-100 text-red-700`;
    
    switch (variant) {
      case "primary":
        return `${base} bg-blue-600 text-white hover:bg-blue-700`;
      case "secondary":
        return `${base} bg-gray-100 text-gray-700 hover:bg-gray-200`;
      case "danger":
        return `${base} bg-red-600 text-white hover:bg-red-700`;
    }
  };

  const renderActionButton = (
    label: string,
    onClick: () => void,
    state: ActionFeedback,
    variant: "primary" | "secondary" | "danger" = "primary"
  ) => (
    <button
      onClick={onClick}
      disabled={state.state === "loading"}
      className={getButtonStyles(state.state, variant)}
    >
      {state.state === "loading" && <span className="animate-spin">*</span>}
      {state.state === "success" && <span>OK</span>}
      {state.state === "error" && <span>X</span>}
      <span>{state.message || label}</span>
    </button>
  );

  // Determine which actions to show based on status
  const showConfirmNs = domain.status === "ns_propagating";
  const showVerifyDns = domain.status === "dns_configuring" || domain.status === "active";
  const isFullyActive = domain.status === "active" && 
    domain.mx_configured && domain.spf_configured && domain.dkim_enabled && domain.dmarc_configured;

  return (
    <div className="space-y-4">
      {/* Next Step Guidance */}
      {!isFullyActive && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h4 className="font-medium text-blue-900 mb-2">Next Step</h4>
          {showConfirmNs && (
            <p className="text-sm text-blue-700">
              Update your nameservers at your domain registrar, then click the button below to confirm.
            </p>
          )}
          {domain.status === "dns_configuring" && (
            <p className="text-sm text-blue-700">
              Email DNS is created by the batch setup wizard after Microsoft shows the required records.
            </p>
          )}
          {showVerifyDns && !isFullyActive && (
            <p className="text-sm text-blue-700">
              Click verify to check if DNS records have propagated.
            </p>
          )}
        </div>
      )}

      {/* Success State */}
      {isFullyActive && (
        <div className="bg-green-50 border border-green-200 rounded-lg p-4">
          <div className="flex items-center gap-3">
            <div className="flex h-10 w-10 items-center justify-center rounded-full bg-green-100">
              <span className="text-green-600 text-lg">OK</span>
            </div>
            <div>
              <h4 className="font-medium text-green-900">Domain Ready!</h4>
              <p className="text-sm text-green-700">
                All DNS records are verified. This domain is ready for email sending.
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Action Buttons */}
      <div className="space-y-3">
        {showConfirmNs && (
          renderActionButton(
            "I've Updated My Nameservers",
            handleConfirmNs,
            confirmNsState,
            "primary"
          )
        )}

        {showVerifyDns && (
          renderActionButton(
            "Verify DNS Records",
            handleVerifyDns,
            verifyDnsState,
            "secondary"
          )
        )}
      </div>

      {/* Danger Zone */}
      <div className="border-t border-gray-200 pt-4 mt-6">
        <h4 className="text-sm font-medium text-gray-500 mb-3">Danger Zone</h4>
        {!showDeleteConfirm ? (
          <button
            onClick={() => setShowDeleteConfirm(true)}
            className="text-sm text-red-600 hover:text-red-700 hover:underline"
          >
            Delete this domain...
          </button>
        ) : (
          <div className="bg-red-50 border border-red-200 rounded-lg p-4 space-y-3">
            <p className="text-sm text-red-700">
              Are you sure? This will remove the domain from Cloudflare and delete all associated records.
            </p>
            <div className="flex gap-3">
              {renderActionButton(
                "Yes, Delete Domain",
                handleDelete,
                deleteState,
                "danger"
              )}
              <button
                onClick={() => setShowDeleteConfirm(false)}
                className="px-4 py-3 text-sm font-medium rounded-lg bg-gray-100 text-gray-700 hover:bg-gray-200"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
